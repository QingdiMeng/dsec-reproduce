"""Bounded local RPC service; backend state belongs to runtime managers."""
import json
import os
import socketserver
import threading
from dsec.runtime.lifecycle import SandboxError, ServiceBusy
from dsec.runtime.requests import MUTATING
from dsec.runtime.resource_rpc import sandbox_resource_sample
from dsec.runtime.admission_guard import AdmissionDenied, check_create
from dsec.runtime.container_edge import CONTAINER_OPERATIONS, CONTAINER_MUTATING

class BoundedServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=False
    block_on_close=True
    request_queue_size=16
    def __init__(self,path,handler,max_requests):
        self.request_queue_size=max(16,max_requests)
        self.slots=threading.BoundedSemaphore(max_requests)
        self.max_requests=max_requests
        self.count_lock=threading.Lock(); self.active_requests=0; self.rejected_requests=0
        super().__init__(path,handler)
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
            if op=="create":
                check_create(self.server.admission_worker_socket,request_id,args)
            if op=="prewarm" and self.server.admission_worker_socket:
                raise AdmissionDenied("Prewarm requires a scheduler-managed pool reservation")
            if op in CONTAINER_OPERATIONS:
                # Container effects have their existing lifecycle/guest journals.
                # Do not wrap them in the microVM journal or change old digests.
                foreground = op in CONTAINER_MUTATING
                if foreground:
                    self.server.manager.foreground_enter()
                try:
                    value=self.server.container_runtime.dispatch(
                        op,req.get("sandbox_id"),args,request_id)
                    response={"request_id":request_id,"ok":True,"result":value}
                finally:
                    if foreground:
                        self.server.manager.foreground_exit()
            elif op in MUTATING:
                self.server.manager.foreground_enter()
                try:
                    cached=self.server.journal.begin(req)
                    if cached is not None:
                        response=cached
                    else:
                        admitted=True
                        try:
                            value=self._dispatch(op,req.get("sandbox_id"),args,request_id)
                            response={"request_id":request_id,"ok":True,"result":value}
                        except Exception as exc:
                            response={"request_id":request_id,"ok":False,"error":{
                                "type":type(exc).__name__,"message":str(exc)}}
                        # A failed durable commit leaves PENDING; never tell the
                        # client that the operation is known to have finished.
                        self.server.journal.finish(request_id,response)
                finally:
                    self.server.manager.foreground_exit()
            else:
                value=self._dispatch(op,req.get("sandbox_id"),args,request_id)
                response={"request_id":request_id,"ok":True,"result":value}
        except Exception as exc:
            if admitted:
                return  # Side effect may have happened; PENDING is authoritative.
            response={"request_id":request_id,"ok":False,"error":{"type":type(exc).__name__,"message":str(exc)}}
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
                    "protocol_features":["container-rpc-v1"],
                    "admission_worker_socket":self.server.admission_worker_socket,**counts}
        if op=="query_request":
            return self.server.journal.lookup(args["lookup_id"])
        if op=="create":
            return manager.create(**args).status()
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
