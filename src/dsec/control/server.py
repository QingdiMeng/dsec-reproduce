"""Bounded local RPC service; backend state belongs to runtime managers."""
from contextlib import nullcontext
import json
import os
import socketserver
import threading
from dsec.runtime.lifecycle import SandboxError, ServiceBusy
from dsec.contracts.resources import NodeAdmissionBusy, NodeLeaseUncertain
from dsec.contracts.sandbox import UnsupportedCapability
from dsec.runtime.requests import MUTATING
from dsec.runtime.resource_rpc import sandbox_resource_sample
from dsec.runtime.admission_guard import AdmissionDenied, check_create
from dsec.runtime.container_edge import CONTAINER_OPERATIONS, CONTAINER_MUTATING
from dsec.contracts.native import NATIVE_FEATURE, NATIVE_MUTATING, STREAM_FEATURE
from dsec.contracts.errors import CommandOutcomeUnknown
from dsec.runtime.sessions.service import dispatch_native
from dsec.runtime.sessions.jobs import NativeJobs

class BoundedServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=False
    block_on_close=True
    request_queue_size=16
    def __init__(self,path,handler,max_requests):
        self.request_queue_size=max(16,max_requests)
        self.slots=threading.BoundedSemaphore(max_requests)
        self.max_requests=max_requests
        self.count_lock=threading.Lock(); self.active_requests=0; self.rejected_requests=0
        self.native_jobs = None
        super().__init__(path,handler)
    def stream_jobs(self):
        with self.count_lock:
            if self.native_jobs is None:
                self.native_jobs = NativeJobs(self)
            return self.native_jobs
    def server_close(self):
        super().server_close()
        if self.native_jobs is not None:
            self.native_jobs.close()
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            with self.count_lock: self.rejected_requests+=1
            try:
                request.settimeout(1)
                request.sendall(json.dumps({"request_id":None,"ok":False,"error":{
                    "type":"ServiceBusy","message":"Request capacity reached; request not accepted"}}).encode()+b"\n")
            except OSError: pass
            self.shutdown_request(request)
            return
        with self.count_lock: self.active_requests+=1
        try:
            super().process_request(request,address)
        except Exception:
            self.slots.release()
            with self.count_lock: self.active_requests-=1
            raise
    def process_request_thread(self,request,address):
        try:
            super().process_request_thread(request,address)
        finally:
            with self.count_lock: self.active_requests-=1
            self.slots.release()

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(45)
        request_id=None
        admitted=False
        try:
            line=self.rfile.readline(131073)
            if len(line)>131072 or not line.endswith(b"\n"):
                raise ValueError("Invalid request frame")
            req=json.loads(line)
            request_id=req.get("request_id")
            op=req["operation"]; args=req.get("args",{})
            node = getattr(self.server, 'node_admission', None)
            if req.get('resource_demand') is not None:
                if node is None:
                    raise UnsupportedCapability('Node admission must be configured for resource demand')
                if op not in ('create','container_create'):
                    raise ValueError('Node demand only applies to creation')
            if op=="create" and node is None:
                check_create(self.server.admission_worker_socket,request_id,args)
            if op=="prewarm" and (self.server.admission_worker_socket or node is not None):
                raise AdmissionDenied("Prewarm requires a scheduler-managed pool reservation")
            if op in CONTAINER_OPERATIONS:
                # Container effects have their existing lifecycle/guest journals.
                # Do not wrap them in the microVM journal or change old digests.
                foreground = op in CONTAINER_MUTATING
                if foreground:
                    self.server.manager.foreground_enter()
                try:
                    value=self.server.container_runtime.dispatch(
                        op,req.get("sandbox_id"),args,request_id,
                        resource_demand=req.get('resource_demand'))
                    response={"request_id":request_id,"ok":True,"result":value}
                finally:
                    if foreground:
                        self.server.manager.foreground_exit()
            elif op == "native_stream":
                value = self.server.stream_jobs().start(req)
                response={"request_id":request_id,"ok":True,"result":value}
            elif op in MUTATING | NATIVE_MUTATING:
                self.server.manager.foreground_enter()
                try:
                    context = (node.request('microvm', request_id, args,
                               req.get('resource_demand')) if op == 'create' and node is not None
                               else nullcontext())
                    with context:
                        cached=self.server.journal.begin(req)
                        if cached is not None:
                            response=cached
                        else:
                            admitted=True
                            commit_response=True
                            try:
                                value=self._dispatch(op,req.get("sandbox_id"),args,request_id)
                                response={"request_id":request_id,"ok":True,"result":value}
                            except (NodeAdmissionBusy, ServiceBusy) as exc:
                                # These errors prove rejection before effects:
                                # budget admission or the sandbox operation
                                # lock. Keep a stable request ID retryable.
                                self.server.journal.reject_before_effect(request_id,operation=op)
                                admitted=False
                                commit_response=False
                                error={"type":type(exc).__name__,"message":str(exc)}
                                if isinstance(exc,NodeAdmissionBusy):
                                    error['details']={'reasons':exc.reasons}
                                response={"request_id":request_id,"ok":False,"error":error}
                            except NodeLeaseUncertain as exc:
                                # Preserve PENDING and report uncertainty now;
                                # a caller must query, never repeat this effect.
                                commit_response=False
                                response={"request_id":request_id,"ok":False,"error":{
                                    "type":"RequestOutcomeUnknown","message":str(exc)}}
                            except CommandOutcomeUnknown as exc:
                                if op not in NATIVE_MUTATING:
                                    response={"request_id":request_id,"ok":False,"error":{
                                        "type":type(exc).__name__,"message":str(exc)}}
                                else:
                                    self.server.journal.unknown(request_id)
                                    commit_response=False
                                    response={"request_id":request_id,"ok":False,"error":{
                                        "type":"RequestOutcomeUnknown","message":str(exc)}}
                            except Exception as exc:
                                response={"request_id":request_id,"ok":False,"error":{
                                    "type":type(exc).__name__,"message":str(exc)}}
                            if commit_response:
                                self.server.journal.finish(request_id,response)
                finally:
                    self.server.manager.foreground_exit()
            else:
                value=self._dispatch(op,req.get("sandbox_id"),args,request_id)
                response={"request_id":request_id,"ok":True,"result":value}
        except Exception as exc:
            if admitted:
                return  # Side effect may have happened; PENDING is authoritative.
            error={"type":type(exc).__name__,"message":str(exc)}
            if isinstance(exc, NodeAdmissionBusy):
                error['details']={'reasons':exc.reasons}
            response={"request_id":request_id,"ok":False,"error":error}
        try:
            self.wfile.write(json.dumps(response).encode()+b"\n")
        except (BrokenPipeError,ConnectionResetError):
            pass

    def _dispatch(self,op,sandbox_id,args,request_id):
        manager=self.server.manager
        if op=="health":
            with self.server.count_lock:
                counts={"active_requests":self.server.active_requests,"max_requests":self.server.max_requests,
                        "rejected_requests":self.server.rejected_requests}
            return {"pid":os.getpid(),"recovery_events":manager.recovery_events,
                    "monitor_errors":manager.errors,"warm_pool":manager.warm_pool_status(),
                    "protocol_features":["container-rpc-v1", "busy-nonadmission-v1", NATIVE_FEATURE, STREAM_FEATURE] + (
                        ["node-admission-v1"] if getattr(self.server, 'node_admission', None) else []),
                    "admission_worker_socket":self.server.admission_worker_socket,**counts}
        if op=="node_status":
            node = getattr(self.server, 'node_admission', None)
            if node is None:
                raise ValueError('Node admission is not configured')
            return node.status()
        if op=="query_request":
            return self.server.journal.lookup(args["lookup_id"])
        if op=="native":
            return dispatch_native(self.server,sandbox_id,args,request_id)
        if op=="native_events":
            return self.server.stream_jobs().events(sandbox_id,args["lookup_id"],args.get("cursor",0),args.get("session_id"))
        if op=="native_cancel":
            return self.server.stream_jobs().cancel(sandbox_id,args,request_id)
        if op=="create":
            sandbox=manager.create(**args)
            node=getattr(self.server, 'node_admission', None)
            if node is not None:
                node.created('microvm', sandbox.id)
            return sandbox.status()
        if op=="prewarm":
            return manager.prewarm(**args)
        if op=="list":
            with manager.lock: sandboxes=list(manager.sandboxes.values())
            value=[]
            for sb in sandboxes:
                if not sb.lock.acquire(blocking=False):
                    # A long guest command holds sb.lock. Expose its last
                    # published state so read-only telemetry does not count a
                    # running VM as zero while that command is executing.
                    state="READY" if sb.reserved and sb.state=="RUNNING" else sb.state
                    value.append({"id":sb.id,"busy":True,"state":state,
                                  "rootfs_transport":manager.tb2_layer_transports.get(
                                      sb.environment_id)})
                else:
                    try: value.append(sb.status())
                    finally: sb.lock.release()
            return value
        if op=="resource_sample":
            return sandbox_resource_sample(manager, sandbox_id, self.server.identity_reader)
        sb=manager.sandboxes.get(sandbox_id)
        if sb is None:
            raise SandboxError("Unknown sandbox")
        if sb.reserved and op not in ("status", "stop"):
            raise SandboxError("Sandbox is reserved for warm checkout")
        if not sb.lock.acquire(blocking=False):
            raise ServiceBusy("Sandbox has another active operation; request not accepted")
        try:
            if (getattr(sb, "native_inflight", {}) and op in
                    ("pause", "resume", "recover", "seal_baseline")):
                raise ServiceBusy("Native work is active; lifecycle operation not admitted")
            if op=="status":
                return sb.status()
            if op in MUTATING:
                sb.inflight={"request_id":request_id,"operation":op}
                sb._persist()
                try:
                    value=getattr(sb,op)(**args)
                    return sb.status() if value is None else value
                finally:
                    sb.inflight=None; sb._persist()
            raise ValueError("Unknown operation")
        finally:
            sb.lock.release()
