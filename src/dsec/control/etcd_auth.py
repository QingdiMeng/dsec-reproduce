"""Bounded, serialized etcd login using a prefix-restricted service identity.

Tokens never appear in repr/logs or on disk here. JWT expiry is a scheduling
hint only; etcd, not this client, verifies token authenticity and permissions.
"""
import base64
import http.client
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request

from dsec._credentials import secret_file
from dsec.contracts.cluster import CoordinationUnavailable


class EtcdCredential:
    def __init__(self, endpoints, opener, *, username, password_file, token=None,
                 timeout=5, renew_before_seconds=300, retry_seconds=5):
        if not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}',username):
            raise ValueError('Explicit etcd service username required')
        if not endpoints or any(not value.startswith('https://') for value in endpoints):
            raise ValueError('Password authentication requires verified HTTPS')
        for value in (renew_before_seconds,retry_seconds):
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<=3600:
                raise ValueError('Finite authentication renewal/retry interval required')
        self.endpoints,self.opener,self.username=tuple(endpoints),opener,username
        self.password_file,self.timeout=password_file,timeout
        self.renew_before,self.retry_seconds=renew_before_seconds,retry_seconds
        self.lock=threading.Lock();self.retry_after=0
        self.token=token;self.expires=self.expiry(token,time.time()) if token else 0
        self.refresh_at=max(0,self.expires-self.renew_before)

    @staticmethod
    def expiry(token,now):
        try:
            body=token.split('.')[1]
            value=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)))['exp']
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
                raise ValueError('Invalid expiry')
            return value
        except (ValueError,IndexError,KeyError,TypeError):
            # Simple tokens carry no expiry; periodically log in rather than
            # assuming they live forever. Rejection still invalidates them.
            return now+600 if token else 0

    def _login(self):
        try:
            password=secret_file(self.password_file)
        except (OSError,ValueError):
            raise CoordinationUnavailable('etcd credential file unavailable or unsafe') from None
        data=json.dumps(dict(name=self.username,password=password)).encode()
        for endpoint in self.endpoints:
            request=urllib.request.Request(endpoint+'/v3/auth/authenticate',data,
                                            {'Content-Type':'application/json'})
            try:
                with self.opener.open(request,timeout=self.timeout) as response:
                    raw=response.read(8193)
                if len(raw)>8192:raise ValueError('Oversized login response')
                token=json.loads(raw)['token']
                if not isinstance(token,str) or not 32<=len(token)<=4096 or '\n' in token or '\r' in token:
                    raise ValueError('Invalid token')
                if self.expiry(token,time.time())<=time.time():raise ValueError('Expired token')
                return token
            except urllib.error.HTTPError as exc:
                if exc.code not in (429,500,502,503,504):
                    raise CoordinationUnavailable('etcd service login rejected') from None
            except (urllib.error.URLError,OSError,http.client.HTTPException):
                continue
            except (ValueError,KeyError,TypeError):
                raise CoordinationUnavailable('Malformed etcd login response') from None
        raise CoordinationUnavailable('No etcd authentication endpoint available')

    def get(self, *, rejected=None):
        with self.lock:
            now=time.time()
            forced=rejected is not None and rejected==self.token
            if forced:
                self.expires=0;self.refresh_at=0
            if self.token and now<self.refresh_at:
                return self.token
            if now<self.retry_after:
                if self.token and now<self.expires:return self.token
                raise CoordinationUnavailable('etcd authentication renewal deferred')
            try:
                token=self._login()
            except CoordinationUnavailable:
                self.retry_after=now+self.retry_seconds
                if self.token and now<self.expires:return self.token
                raise
            self.token=token;self.expires=self.expiry(token,time.time())
            # Short-lived tokens still get a positive useful lifetime, rather
            # than triggering one login for every request.
            margin=min(self.renew_before,max(0,(self.expires-time.time())/2))
            self.refresh_at=self.expires-margin;self.retry_after=0
            return token
