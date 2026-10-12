/* Per-sandbox connection and session proxy. No shell execution here. */
struct ae_client {int fd;size_t used;long deadline;char header[256];};
static struct ae_client ae_pending[NS_CLIENTS];
static void aether_close_pending(int keep) {
    for(unsigned i=0;i<NS_CLIENTS;i++)
        if(ae_pending[i].fd>=0 && ae_pending[i].fd!=keep)close(ae_pending[i].fd);
}
static void aether_orphans(pid_t *children) {
#ifdef __linux__
    char own[96];snprintf(own,sizeof(own),"/proc/self/task/%ld/children",(long)getpid());
    FILE *list=fopen(own,"r");if(!list)return;long pid;
    while(fscanf(list,"%ld",&pid)==1) {
        int known=0;for(unsigned i=0;i<NS_CLIENTS;i++)if(children[i]==pid)known=1;
        if(known)continue;
        char path[80],line[4096];snprintf(path,sizeof(path),"/proc/%ld/stat",pid);
        FILE *f=fopen(path,"r");if(!f)continue;char *read=fgets(line,sizeof(line),f);fclose(f);
        if(!read)continue;
        char *tail=strrchr(line,')');char state;long parent;
        if(tail && sscanf(tail+1," %c %ld",&state,&parent)==2 && parent==getpid()) {
            ns_children((pid_t)pid);kill((pid_t)pid,SIGKILL);
        }
    }
    fclose(list);
#else
    (void)children;
#endif
}
static void aether_forward(int c, const char *header) {
    if(!strcmp(header,"CAP\n")) {
        const char *value="native-sessions-files-v1 native-stream-v1 native-aether-chronus-v1";
        char features[180];snprintf(features,sizeof(features),"%s%s",value,ns_curl()?" native-http-v1":"");
        ns_reply(c,0,0,0,0,0,features,strlen(features),NULL,0);return;
    }
    char action[16],id[33];
    if(sscanf(header,"%15s %32s",action,id)!=2 || !ns_id(id)) {ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);return;}
    /* Legacy sandbox-wide file requests remain supported by a dedicated
     * operation executor. New scoped requests always go to their chronus. */
    if(!strcmp(action,"READ") || action[0]=='W') {ns_role("dsec-chronus");ns_file(c,header);return;}
    char path[160];ns_session_path(path,sizeof(path),id);
    int worker=ns_unix(path,0);
    if(worker<0) {ns_reply(c,ENOENT,0,0,0,1,NULL,0,NULL,0);return;}
    ns_timeout(worker,DSEC_MAX_TIMEOUT_MS+5000);
    if(!strcmp(action,"RUN") || !strcmp(action,"STREAM")) {
        unsigned timeout,limit,length;
        if(sscanf(header,"%*s %*s %u %u %u",&timeout,&limit,&length)!=3 || length>NS_CHUNK) {close(worker);return;}
        size_t h=strlen(header);char *body=malloc(h+length);
        if(body)memcpy(body,header,h);
        if(!body || ns_exact(c,body+h,length,0) || ns_exact(worker,body,h+length,1)) {free(body);close(worker);return;}
        free(body);
    } else if(!strcmp(action,"HTTP")) {
        unsigned timeout,limit,nm,nu,nh,nb;
        if(sscanf(header,"%*s %*s %u %u %u %u %u %u",&timeout,&limit,&nm,&nu,&nh,&nb)!=6 ||
            nm>7 || nu>4096 || nh>8192 || nb>NS_CHUNK) {close(worker);return;}
        size_t h=strlen(header),bytes=(size_t)nm+nu+nh+nb;char *body=malloc(h+bytes);
        if(body)memcpy(body,header,h);
        if(!body || ns_exact(c,body+h,bytes,0) || ns_exact(worker,body,h+bytes,1))
            {free(body);close(worker);return;}
        free(body);
    } else if(!strcmp(action,"SFILE")) {
        const char *file=header+39;unsigned length,pathlen;
        if(strlen(header)<39 || sscanf(file,"%*s %*s %*s %*s %u %u",&length,&pathlen)!=2 ||
                length>NS_CHUNK || !pathlen || pathlen>4096) {close(worker);return;}
        size_t h=strlen(header),bytes=pathlen+(!strncmp(file,"WCHUNK ",7)?length:0);
        char *body=malloc(h+bytes);
        if(body)memcpy(body,header,h);
        if(!body || ns_exact(c,body+h,bytes,0) || ns_exact(worker,body,h+bytes,1))
            {free(body);close(worker);return;}
        free(body);
    } else if(ns_exact(worker,(void *)header,strlen(header),1)) {close(worker);return;}
    char b[4096];ssize_t n;
    /* A subscriber disconnect does not cancel an admitted command. */
    int detached=0;
    while((n=read(worker,b,sizeof(b)))>0) if(!detached && ns_exact(c,b,(size_t)n,1))detached=1;
    close(worker);
}
static pid_t aether_open(int c, const char *id, int server) {
    if(!ns_id(id)) {ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);return -1;}
    char closed[160];snprintf(closed,sizeof(closed),"%s/%.32s.closed",ns_root,id);
    if(!access(closed,F_OK)) {ns_reply(c,EEXIST,0,0,0,1,NULL,0,NULL,0);return -1;}
    DIR *d=opendir(ns_root);unsigned count=0;struct dirent *e;
    if(!d) {ns_reply(c,errno,0,0,0,0,NULL,0,NULL,0);return -1;}
    while((e=readdir(d))) if(strstr(e->d_name,".sock"))count++;
    closedir(d);
    if(count>=NS_SESSIONS) {ns_reply(c,ENOSPC,0,0,0,0,NULL,0,NULL,0);return -1;}
    char path[160];ns_session_path(path,sizeof(path),id);
    int listener=ns_unix(path,1);
    if(listener<0) {ns_reply(c,errno,0,0,0,0,NULL,0,NULL,0);return -1;}
    int ready[2];if(pipe(ready)) {close(listener);unlink(path);ns_reply(c,errno,0,0,0,0,NULL,0,NULL,0);return -1;}
    pid_t parent=getpid(),pid=fork();
    if(!pid) {ns_parent_guard(parent);aether_close_pending(-1);close(server);close(c);close(ready[0]);chronus_serve(listener,ready[1],id);}
    close(listener);close(ready[1]);
    char mark=0;int error=(pid<0 || ns_exact(ready[0],&mark,1,0) || mark!='R')?EIO:0;
    close(ready[0]);if(error)unlink(path);
    ns_reply(c,error,0,0,0,0,NULL,0,NULL,0);
    return pid;
}
static void ns_cleanup(void) {
    DIR *d=opendir(ns_root);if(!d)return;
    struct dirent *e;
    while((e=readdir(d))) {
        if(e->d_name[0]=='.')continue;
        char path[512];snprintf(path,sizeof(path),"%s/%s",ns_root,e->d_name);
        if(strstr(e->d_name,".upload")) {
            struct ns_upload u;int fd=open(path,O_RDONLY|O_NOFOLLOW);
            if(fd>=0) {
                if(!ns_exact(fd,&u,sizeof(u),0) && memchr(u.path,0,sizeof(u.path))) {
                    char *stage=NULL;
                    if(asprintf(&stage,"%s.dsec-upload-%.32s",u.path,e->d_name)>=0)unlink(stage);
                    free(stage);
                }
                close(fd);
            }
        }
        unlink(path);rmdir(path);
    }
    closedir(d);
    char guard[160];snprintf(guard,sizeof(guard),"%s/.uploads.lock",ns_root);unlink(guard);
    rmdir(ns_root);
}
int dsec_native_serve(int server, const char *root) {
    ns_role("dsec-aether");
    if(fcntl(server,F_SETFL,fcntl(server,F_GETFL)|O_NONBLOCK)<0)return 1;
    signal(SIGPIPE,SIG_IGN);
#ifdef __linux__
    if(prctl(PR_SET_CHILD_SUBREAPER,1))return 1;
#endif
    struct sigaction sa={0};sa.sa_handler=ns_signal;sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM,&sa,NULL);sigaction(SIGINT,&sa,NULL);
    sa.sa_handler=ns_child_signal;sigaction(SIGCHLD,&sa,NULL);
    if(root && (chdir(root) || chroot(root) || chdir("/")))return 1;
    strcpy(ns_root,"/tmp/dsec-native-XXXXXX");
    if(!mkdtemp(ns_root))return 1;
    pid_t children[NS_CLIENTS]={0};
    char sessions[NS_CLIENTS][33]={{0}};
    for(unsigned i=0;i<NS_CLIENTS;i++)ae_pending[i].fd=-1;
    while(!ns_stopping) {
        pid_t reaped;
        while((reaped=waitpid(-1,NULL,WNOHANG))>0)
            for(unsigned i=0;i<NS_CLIENTS;i++)if(children[i]==reaped) {
                children[i]=0;
                if(sessions[i][0]) {
                    char path[160];ns_session_path(path,sizeof(path),sessions[i]);
                    ns_upload_cleanup(sessions[i]);
                    ns_retire_session(sessions[i]);
                    unlink(path);sessions[i][0]=0;
                }
            }
        aether_orphans(children);
        struct pollfd incoming[NS_CLIENTS+1]={{.fd=server,.events=POLLIN}};
        for(unsigned i=0;i<NS_CLIENTS;i++)
            incoming[i+1]=(struct pollfd){.fd=ae_pending[i].fd,.events=POLLIN};
        (void)poll(incoming,NS_CLIENTS+1,100);
        if(ns_stopping)break;
        if(incoming[0].revents & POLLIN) {
            int c=ns_accept(server);
            if(c>=0) {
                unsigned slot=0;while(slot<NS_CLIENTS && ae_pending[slot].fd>=0)slot++;
                ns_timeout(c,5000);
                if(slot==NS_CLIENTS) {ns_reply(c,EBUSY,0,0,0,0,NULL,0,NULL,0);close(c);}
                else ae_pending[slot]=(struct ae_client){.fd=c,.deadline=ns_now()+5000};
            }
        }
        for(unsigned i=0;i<NS_CLIENTS && !ns_stopping;i++) {
            struct ae_client *client=&ae_pending[i];
            if(client->fd<0)continue;
            if(ns_now()>=client->deadline) {close(client->fd);client->fd=-1;continue;}
            if(!(incoming[i+1].revents & (POLLIN|POLLHUP|POLLERR)))continue;
            int complete=0,bad=0;
            while(client->used+1<sizeof(client->header)) {
                char ch;ssize_t n=recv(client->fd,&ch,1,MSG_DONTWAIT);
                if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK))break;
                if(n<0 && errno==EINTR && !ns_stopping)continue;
                if(n<=0 || ch==0) {bad=1;break;}
                client->header[client->used++]=ch;
                if(ch=='\n') {complete=1;break;}
            }
            if(!complete && client->used+1==sizeof(client->header))bad=1;
            if(bad) {close(client->fd);client->fd=-1;continue;}
            if(!complete || ns_stopping)continue;
            client->header[client->used]=0;
            int c=client->fd;client->fd=-1;
            const char *h=client->header;
            unsigned slot=0;while(slot<NS_CLIENTS && children[slot])slot++;
            if(slot==NS_CLIENTS)ns_reply(c,EBUSY,0,0,0,0,NULL,0,NULL,0);
            else if(!strncmp(h,"OPEN ",5)) {
                char id[33],extra;
                if(sscanf(h,"OPEN %32s %c",id,&extra)!=1 || !ns_id(id))
                    ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);
                else {
                    pid_t p=aether_open(c,id,server);
                    if(p>0) {children[slot]=p;strcpy(sessions[slot],id);}
                }
            } else {
                pid_t parent=getpid(),p=fork();
                if(!p) {ns_parent_guard(parent);ns_role("dsec-relay");aether_close_pending(c);
                    close(server);aether_forward(c,h);close(c);_exit(0);}
                if(p>0)children[slot]=p;else ns_reply(c,errno,0,0,0,0,NULL,0,NULL,0);
            }
            close(c);
        }
    }
    aether_close_pending(-1);
    for(unsigned i=0;i<NS_CLIENTS;i++)if(children[i])kill(children[i],SIGTERM);
    for(unsigned i=0;i<NS_CLIENTS;i++)if(children[i]) {
        long deadline=ns_now()+1000;
        while(waitpid(children[i],NULL,WNOHANG)==0 && ns_now()<deadline)usleep(1000);
        if(waitpid(children[i],NULL,WNOHANG)==0) {
            kill(children[i],SIGKILL);waitpid(children[i],NULL,0);
        }
    }
    close(server);ns_cleanup();return 0;
}
#ifdef DSEC_NATIVE_STANDALONE
int main(int argc,char **argv) {
    if(argc!=3 && argc!=5) {fprintf(stderr,"usage: agent --unix PATH [--root ROOT]\n");return 2;}
    if(strcmp(argv[1],"--unix") || (argc==5 && strcmp(argv[3],"--root")))return 2;
    int fd=ns_unix(argv[2],1);if(fd<0) {perror("native socket");return 1;}
    /* Parent directory is private; Edge and the container's root differ in UID. */
    if(chmod(argv[2],0666))return 1;
    return dsec_native_serve(fd,argc==5?argv[4]:NULL);
}
#else
int dsec_native_spawn(void) {
#ifdef __linux__
    int fd=socket(AF_VSOCK,SOCK_STREAM|SOCK_CLOEXEC,0);
    struct sockaddr_vm a={.svm_family=AF_VSOCK,.svm_port=5001,.svm_cid=VMADDR_CID_ANY};
    if(fd<0 || bind(fd,(void *)&a,sizeof(a)) || listen(fd,16)) {if(fd>=0)close(fd);return -1;}
    pid_t pid=fork();
    if(!pid) _exit(dsec_native_serve(fd,NULL));
    close(fd);return pid<0?-1:0;
#else
    return -1;
#endif
}
#endif
