/* Native v1 guest service. This file is also built as a standalone UDS agent.
 * Edge owns the durable intent/result journal; this service never retries work.
 * The legacy port 5000 remains in guest_agent.c. Native microVM port is 5001.
 */
#define _GNU_SOURCE
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/file.h>
#ifdef __linux__
#include <linux/vm_sockets.h>
#include <sys/prctl.h>
#endif
#include <dirent.h>
#include <poll.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>

#ifndef DSEC_MAX_TIMEOUT_MS
#define DSEC_MAX_TIMEOUT_MS 30000
#endif
#define NS_CHUNK 65536
#define NS_OUTPUT 1048576
#define NS_FILE (64*1024*1024)
#define NS_SESSIONS 32
#define NS_CLIENTS 64
static char ns_root[100];
static volatile sig_atomic_t ns_stopping;
static pid_t ns_shell;
static int ns_stream_client=-1;
static void ns_parent_guard(pid_t parent) {
#ifdef __linux__
    if(prctl(PR_SET_PDEATHSIG,SIGTERM) || getppid()!=parent)_exit(1);
#else
    (void)parent;
#endif
}
static long ns_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec*1000+t.tv_nsec/1000000;
}
static int ns_exact(int fd, void *ptr, size_t n, int writing) {
    char *p=ptr;
    while(n) {
        ssize_t k=writing?write(fd,p,n):read(fd,p,n);
        if(k<0 && errno==EINTR && !ns_stopping) continue;
        if(k<=0) return -1;
        p+=k; n-=(size_t)k;
    }
    return 0;
}
static int ns_line(int fd, char *p, size_t size) {
    size_t n=0;
    while(n+1<size) {
        if(ns_exact(fd,p+n,1,0)) return -1;
        if(p[n++]=='\n') {p[n]=0; return 0;}
    }
    return -1;
}
static void ns_timeout(int fd, unsigned ms) {
    struct timeval tv={.tv_sec=ms/1000,.tv_usec=(ms%1000)*1000};
    setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&tv,sizeof(tv));
    setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&tv,sizeof(tv));
}
static int ns_reply(int fd, int error, int code, int timeout, int truncated,
                    int reset, const char *out, size_t no, const char *err, size_t ne) {
    char h[128];
    int cancelled=reset && code==125 && !timeout;
    int n=snprintf(h,sizeof(h),"DSEC1 %d %d %d %d %d %d %zu %zu\n",error,code,
                   timeout,truncated,reset,cancelled,no,ne);
    if(ns_exact(fd,h,(size_t)n,1)) return -1;
    if(no && ns_exact(fd,(void *)out,no,1)) return -1;
    if(ne && ns_exact(fd,(void *)err,ne,1)) return -1;
    return 0;
}
static int ns_id(const char *s) {
    if(strlen(s)!=32) return 0;
    for(int i=0;i<32;i++) if(!((s[i]>='0' && s[i]<='9') || (s[i]>='a' && s[i]<='f'))) return 0;
    return 1;
}
static int ns_unix(const char *path, int bind_socket) {
    struct sockaddr_un a={.sun_family=AF_UNIX};
    if(strlen(path)>=sizeof(a.sun_path)) {errno=ENAMETOOLONG; return -1;}
    strcpy(a.sun_path,path);
    int fd=socket(AF_UNIX,SOCK_STREAM,0);
    if(fd<0) return -1;
    fcntl(fd,F_SETFD,FD_CLOEXEC);
    if((bind_socket?bind(fd,(void *)&a,sizeof(a)):connect(fd,(void *)&a,sizeof(a)))) {
        int e=errno; close(fd); errno=e; return -1;
    }
    if(bind_socket && listen(fd,8)) {close(fd); return -1;}
    return fd;
}
static void ns_session_path(char *path, size_t size, const char *id) {
    snprintf(path,size,"%s/%s.sock",ns_root,id);
}
static int ns_accept(int listener) {
    int fd=accept(listener,NULL,NULL);
    if(fd>=0 && fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)&~O_NONBLOCK)<0) {
        int saved=errno;close(fd);errno=saved;return -1;
    }
    return fd;
}
static void ns_signal(int sig) { (void)sig; ns_stopping=1; if(ns_shell>0) kill(-ns_shell,SIGKILL); }
static void ns_child_signal(int sig) { (void)sig; }
#ifdef __linux__
/* Commands cannot leave detached work outside the session's lifetime. */
static void ns_children(pid_t parent) {
    DIR *dir=opendir("/proc"); if(!dir) return;
    struct dirent *item;
    while((item=readdir(dir))) {
        char *end; long pid=strtol(item->d_name,&end,10);
        if(*end || pid<=0) continue;
        char path[80],line[4096]; snprintf(path,sizeof(path),"/proc/%ld/stat",pid);
        FILE *f=fopen(path,"r"); if(!f) continue;
        char *read=fgets(line,sizeof(line),f); fclose(f); if(!read) continue;
        char *tail=strrchr(line,')'); char state; long ppid;
        if(tail && sscanf(tail+1," %c %ld",&state,&ppid)==2 && ppid==parent) {
            ns_children((pid_t)pid); kill((pid_t)pid,SIGKILL);
        }
    }
    closedir(dir);
}
#else
static void ns_children(pid_t parent) { (void)parent; }
#endif
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
static void ns_session(int listener, int readyfd, const char *id) {
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
            if(!strncmp(h,"RUN ",4) || !strncmp(h,"STREAM ",7)) ns_stopping=ns_run(c,listener,input[1],done[0],h,dir);
            else if(!strncmp(h,"CLOSE ",6)) {unlink(path);ns_reply(c,0,0,0,0,0,NULL,0,NULL,0);ns_stopping=1;}
            else if(!strncmp(h,"CANCEL ",7)) ns_reply(c,ENOENT,0,0,0,0,NULL,0,NULL,0);
            else ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);
        }
        close(c);
    }
    ns_children(ns_shell);kill(-ns_shell,SIGKILL);
    while(waitpid(ns_shell,NULL,0)<0 && errno==EINTR) {}
    close(input[1]);close(done[0]);close(listener);unlink(path);
    char fifo[180];snprintf(fifo,sizeof(fifo),"%s/out",dir);unlink(fifo);
    snprintf(fifo,sizeof(fifo),"%s/err",dir);unlink(fifo);rmdir(dir);_exit(0);
}
struct ns_upload {uint64_t total,next;unsigned mode;char path[4097];};
static uint64_t ns_stamp(struct stat *s,int ctime) {
#ifdef __APPLE__
    struct timespec t=ctime?s->st_ctimespec:s->st_mtimespec;
#else
    struct timespec t=ctime?s->st_ctim:s->st_mtim;
#endif
    return (uint64_t)t.tv_sec*1000000000+(uint64_t)t.tv_nsec;
}
static void ns_u64(char *out,uint64_t value) {
    for(int i=7;i>=0;i--) {out[i]=(char)(value&255);value>>=8;}
}
static int ns_upload_budget(uint64_t total) {
    DIR *d=opendir(ns_root);if(!d)return errno;
    uint64_t reserved=total;unsigned count=0;struct dirent *e;int error=0;
    while((e=readdir(d))) {
        if(e->d_name[0]=='.' || !strstr(e->d_name,".upload"))continue;
        char path[512];snprintf(path,sizeof(path),"%s/%s",ns_root,e->d_name);
        int fd=open(path,O_RDONLY|O_NOFOLLOW);struct ns_upload u;
        if(fd<0) {error=errno;break;}
        if(ns_exact(fd,&u,sizeof(u),0)) {close(fd);error=EIO;break;}
        close(fd);reserved+=u.total;count++;
    }
    closedir(d);
    return error?error:(count>=32 || reserved>NS_FILE)?ENOSPC:0;
}
static void ns_file(int c, const char *header) {
    char action[16],id[33];unsigned long long offset,total;unsigned length,pathlen,mode;
    if(sscanf(header,"%15s %32s %llu %llu %u %u %u",action,id,&offset,&total,&length,&pathlen,&mode)!=7 ||
       !ns_id(id) || pathlen<1 || pathlen>4096 || length>NS_CHUNK || total>NS_FILE ||
       offset>NS_FILE || mode>0777) {ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);return;}
    char path[4097];if(ns_exact(c,path,pathlen,0))return;path[pathlen]=0;
    if(path[0]!='/' || memchr(path,0,pathlen)) {ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);return;}
    int error=0,code=0,fd=-1,meta=-1,guard=-1;size_t used=0;char *buffer=malloc(NS_CHUNK+40);
    char *stage=NULL;char mp[160];snprintf(mp,sizeof(mp),"%s/%s.upload",ns_root,id);
    struct ns_upload u={0};
    if(!buffer || asprintf(&stage,"%s.dsec-upload-%s",path,id)<0) {error=ENOMEM;goto finish;}
    if(!strcmp(action,"READ")) {
        fd=open(path,O_RDONLY|O_CLOEXEC);
        if(fd<0) {error=errno;goto finish;}
        struct stat st;if(fstat(fd,&st) || !S_ISREG(st.st_mode)) {error=EINVAL;goto finish;}
        ssize_t n=pread(fd,buffer+40,length,(off_t)offset);
        struct stat after;
        if(n<0)error=errno;
        else if(fstat(fd,&after) || st.st_size!=after.st_size ||
                ns_stamp(&st,0)!=ns_stamp(&after,0) || ns_stamp(&st,1)!=ns_stamp(&after,1))error=ESTALE;
        else {
            ns_u64(buffer,(uint64_t)st.st_dev);ns_u64(buffer+8,(uint64_t)st.st_ino);
            ns_u64(buffer+16,(uint64_t)st.st_size);ns_u64(buffer+24,ns_stamp(&st,0));
            ns_u64(buffer+32,ns_stamp(&st,1));used=(size_t)n+40;
        }
        goto finish;
    }
    if(!strcmp(action,"WBEGIN")) {
        char gp[160];snprintf(gp,sizeof(gp),"%s/.uploads.lock",ns_root);
        guard=open(gp,O_RDWR|O_CREAT|O_CLOEXEC,0600);
        if(guard<0 || flock(guard,LOCK_EX)) {error=errno;goto finish;}
        error=ns_upload_budget(total);if(error)goto finish;
        meta=open(mp,O_RDWR|O_CREAT|O_EXCL|O_CLOEXEC,0600);
        if(meta<0) {error=errno;goto finish;}
        fd=open(stage,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
        if(fd<0) {error=errno;unlink(mp);goto finish;}
        u.total=total;u.mode=mode;strcpy(u.path,path);
        if(ns_exact(meta,&u,sizeof(u),1) || fsync(meta)) {error=errno?errno:EIO;unlink(stage);unlink(mp);}
        goto finish;
    }
    meta=open(mp,O_RDWR|O_NOFOLLOW|O_CLOEXEC);
    if(meta<0) {error=errno;goto finish;}
    if(flock(meta,LOCK_EX) || ns_exact(meta,&u,sizeof(u),0)) {error=errno?errno:EIO;goto finish;}
    if(strcmp(u.path,path)) {error=EINVAL;goto finish;}
    if(!strcmp(action,"WABORT")) {
        if(unlink(stage) && errno!=ENOENT)error=errno;
        if(!error && unlink(mp))error=errno;
        goto finish;
    }
    if(u.total!=total || u.mode!=mode) {error=EINVAL;goto finish;}
    fd=open(stage,O_RDWR|O_NOFOLLOW|O_CLOEXEC);if(fd<0) {error=errno;goto finish;}
    if(!strcmp(action,"WCHUNK")) {
        if(!length || offset!=u.next || offset+length>u.total) {error=EINVAL;goto finish;}
        if(ns_exact(c,buffer,length,0)) {error=EIO;goto finish;}
        if(pwrite(fd,buffer,length,(off_t)offset)!=(ssize_t)length || fsync(fd)) {error=errno?errno:EIO;goto finish;}
        u.next+=length;
        if(lseek(meta,0,SEEK_SET)<0 || ns_exact(meta,&u,sizeof(u),1) || fsync(meta)) error=errno?errno:EIO;
    } else if(!strcmp(action,"WCOMMIT")) {
        if(u.next!=u.total) {error=EINVAL;goto finish;}
        if(fchmod(fd,mode) || fsync(fd) || rename(stage,path)) {error=errno;goto finish;}
        char *slash=strrchr(stage,'/');*slash=0;
        int dirfd=open(*stage?stage:"/",O_RDONLY|O_DIRECTORY);
        if(dirfd<0 || fsync(dirfd)) {error=errno;code=-2;}
        if(dirfd>=0)close(dirfd);
        if(!error)unlink(mp);
    } else error=EINVAL;
finish:
    if(fd>=0)close(fd);
    if(meta>=0)close(meta);
    if(guard>=0)close(guard);
    ns_reply(c,error,code,0,0,0,buffer,used,NULL,0);free(stage);free(buffer);
}
static void ns_request(int c, const char *header) {
    if(!strcmp(header,"CAP\n")) {
        const char *value="native-sessions-files-v1 native-stream-v1";
        ns_reply(c,0,0,0,0,0,value,strlen(value),NULL,0);return;
    }
    char action[16],id[33];
    if(sscanf(header,"%15s %32s",action,id)!=2 || !ns_id(id)) {ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);return;}
    if(!strcmp(action,"READ") || action[0]=='W') {ns_file(c,header);return;}
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
    } else if(ns_exact(worker,(void *)header,strlen(header),1)) {close(worker);return;}
    char b[4096];ssize_t n;
    /* A subscriber disconnect does not cancel an admitted command. */
    int detached=0;
    while((n=read(worker,b,sizeof(b)))>0) if(!detached && ns_exact(c,b,(size_t)n,1))detached=1;
    close(worker);
}
static pid_t ns_open(int c, const char *id, int server) {
    if(!ns_id(id)) {ns_reply(c,EINVAL,0,0,0,0,NULL,0,NULL,0);return -1;}
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
    if(!pid) {ns_parent_guard(parent);close(server);close(c);close(ready[0]);ns_session(listener,ready[1],id);}
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
    while(!ns_stopping) {
        pid_t reaped;
        while((reaped=waitpid(-1,NULL,WNOHANG))>0)
            for(unsigned i=0;i<NS_CLIENTS;i++)if(children[i]==reaped)children[i]=0;
        struct pollfd incoming={.fd=server,.events=POLLIN};
        if(poll(&incoming,1,100)<=0 || ns_stopping)continue;
        int c=ns_accept(server);if(c<0)continue;
        ns_timeout(c,5000);char h[256];
        if(ns_line(c,h,sizeof(h))) {close(c);continue;}
        unsigned slot=0;while(slot<NS_CLIENTS && children[slot])slot++;
        if(slot==NS_CLIENTS) ns_reply(c,EBUSY,0,0,0,0,NULL,0,NULL,0);
        else if(!strncmp(h,"OPEN ",5)) {
            char id[33];if(sscanf(h,"OPEN %32s",id)==1) {
                pid_t p=ns_open(c,id,server);if(p>0)children[slot]=p;
            }
        }
        else {
            pid_t parent=getpid(),p=fork();
            if(!p) {ns_parent_guard(parent);close(server);ns_request(c,h);close(c);_exit(0);}
            if(p>0)children[slot]=p;else ns_reply(c,errno,0,0,0,0,NULL,0,NULL,0);
        }
        close(c);
    }
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
