/* One persistent shell-session executor; never owns the Edge journal. */
static char *ns_quote(const char *s) {
    size_t n=3;
    for(const char *p=s;*p;p++) n+=(*p=='\''?4:1);
    char *q=malloc(n); if(!q) return NULL;
    char *w=q; *w++='\'';
    for(;*s;s++) {if(*s=='\'') {memcpy(w,"'\\''",4);w+=4;} else *w++=*s;}
    *w++='\''; *w=0; return q;
}
static void ns_drain(int fd, char *buffer, size_t *used, size_t *remaining, int *truncated, const char *kind) {
    char b[4096]; ssize_t n;
    for(unsigned reads=0;reads<16 && (n=read(fd,b,sizeof(b)))>0;reads++) {
        size_t take=(size_t)n<*remaining?(size_t)n:*remaining;
        memcpy(buffer+*used,b,take); *used+=take; *remaining-=take;
        if(take && ns_stream_client>=0) {
            char header[80];int size=snprintf(header,sizeof(header),"DSECE %s %zu\n",kind,take);
            if(ns_exact(ns_stream_client,header,(size_t)size,1) || ns_exact(ns_stream_client,b,take,1))
                ns_stream_client=-1;
        }
        if(take<(size_t)n) *truncated=1;
    }
}
static int ns_run(int client, int listener, int input, int donefd, const char *header,
                  const char *session_dir) {
    char action[16],id[33],operation[33]; unsigned timeout,limit,length;
    if(sscanf(header,"%15s %32s %u %u %u %32s",action,id,&timeout,&limit,&length,operation)!=6 ||
       !ns_id(operation) ||
       !ns_id(id) || timeout<1 || timeout>DSEC_MAX_TIMEOUT_MS || !limit ||
       limit>NS_OUTPUT || length>NS_CHUNK) {ns_reply(client,EINVAL,0,0,0,0,NULL,0,NULL,0);return 0;}
    char *command=calloc((size_t)length+1,1),*out=malloc(limit),*err=malloc(limit);
    if(!command || !out || !err) {free(command);free(out);free(err);ns_reply(client,ENOMEM,0,0,0,0,NULL,0,NULL,0);return 0;}
    if(ns_exact(client,command,length,0) || memchr(command,0,length)) {free(command);free(out);free(err);return 0;}
    int streaming=!strcmp(action,"STREAM");
    ns_stream_client=streaming?client:-1;
    if(streaming)ns_timeout(client,100);
    char po[192],pe[192]; snprintf(po,sizeof(po),"%s/out",session_dir);snprintf(pe,sizeof(pe),"%s/err",session_dir);
    unlink(po);unlink(pe);
    if(mkfifo(po,0600) || mkfifo(pe,0600)) goto setup_error;
    int fo=open(po,O_RDWR|O_NONBLOCK|O_CLOEXEC),fe=open(pe,O_RDWR|O_NONBLOCK|O_CLOEXEC);
    if(fo<0 || fe<0) {if(fo>=0)close(fo);if(fe>=0)close(fe);goto setup_error;}
    char *quoted=ns_quote(command),*qo=ns_quote(po),*qe=ns_quote(pe),*script=NULL;
    if(!quoted || !qo || !qe || asprintf(&script,"eval %s < /dev/null > %s 2> %s\ncommand printf '%%s\\n' \"$?\" >&3\n",quoted,qo,qe)<0) {
        free(quoted);free(qo);free(qe);close(fo);close(fe);goto setup_error;
    }
    free(quoted);free(qo);free(qe);
    size_t no=0,ne=0,remaining=limit; int truncated=0,timedout=0,reset=0,code=0;
    char status[64]={0};size_t sn=0; int complete=0;
    long deadline=ns_now()+timeout;
    if(ns_exact(input,script,strlen(script),1)) {reset=1;complete=1;code=-1;}
    free(script);
    while(!complete && !ns_stopping) {
        ns_drain(fo,out,&no,&remaining,&truncated,"stdout");ns_drain(fe,err,&ne,&remaining,&truncated,"stderr");
        ssize_t n=read(donefd,status+sn,sizeof(status)-1-sn);
        if(n>0) {sn+=(size_t)n;if(memchr(status,'\n',sn)) {code=atoi(status);complete=1;}}
        else if(!n) {
            reset=1;code=-1;complete=1;
            int st;
            if(waitpid(ns_shell,&st,WNOHANG)==ns_shell)
                code=WIFEXITED(st)?WEXITSTATUS(st):128+WTERMSIG(st);
        }
        if(ns_now()>=deadline && !complete) {timedout=1;reset=1;code=124;complete=1;kill(-ns_shell,SIGKILL);}
        struct pollfd p[4]={{fo,POLLIN,0},{fe,POLLIN,0},{donefd,POLLIN,0},{listener,POLLIN,0}};
        poll(p,4,5);
        if(p[3].revents & POLLIN) {
            int other=ns_accept(listener);
            if(other>=0) {
                ns_timeout(other,1000);char h[256];
                if(!ns_line(other,h,sizeof(h))) {
                    char target[33],cancel_session[33];
                    int cancel=sscanf(h,"CANCEL %32s %32s",cancel_session,target)==2;
                    if(!strncmp(h,"CLOSE ",6) || (cancel && !strcmp(target,operation))) {
                        ns_retire_session(id);
                        char closing[180];snprintf(closing,sizeof(closing),"%s.sock",session_dir);
                        unlink(closing);
                        ns_stopping=1;reset=1;code=125;ns_reply(other,0,0,0,0,1,NULL,0,NULL,0);
                    }
                    else if(cancel)ns_reply(other,ENOENT,0,0,0,0,NULL,0,NULL,0);
                    else ns_reply(other,EBUSY,0,0,0,0,NULL,0,NULL,0);
                }
                close(other);
            }
        }
    }
    if(ns_stopping && !complete) {reset=1;code=125;}
    ns_children(ns_shell);
    ns_drain(fo,out,&no,&remaining,&truncated,"stdout");ns_drain(fe,err,&ne,&remaining,&truncated,"stderr");
    close(fo);close(fe);unlink(po);unlink(pe);
    if(reset || ns_stopping) {
        char closing[180];snprintf(closing,sizeof(closing),"%s.sock",session_dir);unlink(closing);
    }
    ns_reply(client,0,code,timedout,truncated,reset,out,streaming?0:no,err,streaming?0:ne);
    ns_stream_client=-1;
    free(command);free(out);free(err);
    return reset || ns_stopping;
setup_error:
    ns_reply(client,errno?errno:ENOMEM,0,0,0,0,NULL,0,NULL,0);
    unlink(po);unlink(pe);free(command);free(out);free(err);return 0;
}
static void chronus_serve(int listener, int readyfd, const char *id) {
    ns_role("dsec-chronus");
    strcpy(ns_session_id,id);
    fcntl(listener,F_SETFL,fcntl(listener,F_GETFL)|O_NONBLOCK);
#ifdef __linux__
    if(prctl(PR_SET_CHILD_SUBREAPER,1))_exit(1);
#endif
    char dir[160],path[160];snprintf(dir,sizeof(dir),"%s/%s",ns_root,id);
    ns_session_path(path,sizeof(path),id);
    if(mkdir(dir,0700)) _exit(1);
    int input[2],done[2]; if(pipe(input) || pipe(done)) _exit(1);
    pid_t parent=getpid();ns_shell=fork();
    if(!ns_shell) {
        ns_parent_guard(parent);
        setpgid(0,0);
        dup2(input[0],0);dup2(done[1],3);
        int null=open("/dev/null",O_WRONLY);dup2(null,1);dup2(null,2);
        /* Preserve fd 3 even when one original pipe descriptor has that number. */
        for(int fd=4;fd<1024;fd++) close(fd);
        execl("/bin/sh","sh",(char *)NULL);_exit(127);
    }
    close(input[0]);close(done[1]);
    if(ns_shell<0) _exit(1);
    setpgid(ns_shell,ns_shell);
    fcntl(done[0],F_SETFL,O_NONBLOCK);
    struct sigaction sa={0};sa.sa_handler=ns_signal;sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM,&sa,NULL);sigaction(SIGINT,&sa,NULL);
    ns_exact(readyfd,"R",1,1);close(readyfd);
    while(!ns_stopping) {
        pid_t reaped;
        while((reaped=waitpid(-1,NULL,WNOHANG))>0)if(reaped==ns_shell)ns_stopping=1;
        if(ns_stopping)break;
        struct pollfd incoming={.fd=listener,.events=POLLIN};
        if(poll(&incoming,1,100)<=0 || ns_stopping)continue;
        int c=ns_accept(listener);
        if(c<0) {if(errno==EINTR)continue;break;}
        ns_timeout(c,DSEC_MAX_TIMEOUT_MS+5000);
        char h[256];
        if(!ns_line(c,h,sizeof(h))) {
            if(!strncmp(h,"RUN ",4) || !strncmp(h,"STREAM ",7)) {
                NS_TEST_BEFORE_RUN();
                int stop_run=ns_run(c,listener,input[1],done[0],h,dir);
                NS_TEST_AFTER_RUN(stop_run);
                /* A signal may have requested stop after ns_run returned.
                 * Never write zero here, including through a read-modify-write. */
                if(stop_run)ns_stopping=1;
                NS_TEST_AFTER_APPLY();
            }
            else if(!strncmp(h,"HTTP ",5)) {if(ns_http(c,listener,h,dir))ns_stopping=1;}
            else if(!strncmp(h,"SFILE ",6) && strlen(h)>39) ns_file(c,h+39);
            else if(!strncmp(h,"CLOSE ",6)) {ns_retire_session(id);unlink(path);ns_reply(c,0,0,0,0,0,NULL,0,NULL,0);ns_stopping=1;}
            else if(!strncmp(h,"CANCEL ",7)) ns_reply(c,ENOENT,0,0,0,0,NULL,0,NULL,0);
            else ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);
        }
        close(c);
    }
    ns_retire_session(id);
    /* The subreaper also owns double-forked/setsid descendants. Walking only
     * the shell misses processes already reparented to this chronus. */
    ns_children(getpid());kill(-ns_shell,SIGKILL);
    while(waitpid(ns_shell,NULL,0)<0 && errno==EINTR) {}
    close(input[1]);close(done[0]);close(listener);unlink(path);
    ns_upload_cleanup(id);
    char fifo[180];snprintf(fifo,sizeof(fifo),"%s/out",dir);unlink(fifo);
    snprintf(fifo,sizeof(fifo),"%s/err",dir);unlink(fifo);rmdir(dir);_exit(0);
}
