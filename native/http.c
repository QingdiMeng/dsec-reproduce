/* Native HTTP execution lives in chronus. curl is exec'd directly, never via
 * a shell; -q disables task-controlled curlrc. Guest networking remains the
 * sandbox network policy. This optional capability is advertised only when a
 * guest curl executable exists. */
#include <sys/resource.h>
static const char *ns_curl(void) {
    if(!access("/usr/bin/curl",X_OK))return "/usr/bin/curl";
    if(!access("/bin/curl",X_OK))return "/bin/curl";
    return NULL;
}
static int ns_http(int client,int listener,const char *header,const char *dir) {
    char id[33],operation[33];unsigned timeout,limit,nm,nu,nh,nb;
    if(sscanf(header,"HTTP %32s %u %u %u %u %u %u %32s",id,&timeout,&limit,&nm,&nu,&nh,&nb,operation)!=8 ||
        !ns_id(id) || !ns_id(operation) || !timeout || timeout>DSEC_MAX_TIMEOUT_MS ||
        !limit || limit>NS_OUTPUT-8192 || nm<3 || nm>7 || !nu || nu>4096 || nh>8192 || nb>NS_CHUNK)
        {ns_reply(client,EINVAL,0,0,0,0,NULL,0,NULL,0);return 0;}
    const char *curl=ns_curl();if(!curl) {ns_reply(client,ENOTSUP,0,0,0,0,NULL,0,NULL,0);return 0;}
    size_t bytes=(size_t)nm+nu+nh+nb;char *wire=malloc(bytes);
    if(!wire) {ns_reply(client,ENOMEM,0,0,0,0,NULL,0,NULL,0);return 0;}
    if(ns_exact(client,wire,bytes,0)) {free(wire);return 0;}
    char method[8],url[4097],headers[8193];
    memcpy(method,wire,nm);method[nm]=0;memcpy(url,wire+nm,nu);url[nu]=0;
    memcpy(headers,wire+nm+nu,nh);headers[nh]=0;
    if(memchr(method,0,nm) || memchr(url,0,nu) || memchr(headers,0,nh) ||
        (strncmp(url,"http://",7) && strncmp(url,"https://",8)) ||
        (strcmp(method,"GET") && strcmp(method,"HEAD") && strcmp(method,"POST") &&
         strcmp(method,"PUT") && strcmp(method,"PATCH") && strcmp(method,"DELETE") && strcmp(method,"OPTIONS")))
        {free(wire);ns_reply(client,EINVAL,0,0,0,0,NULL,0,NULL,0);return 0;}
    char tmp[256];snprintf(tmp,sizeof(tmp),"%s/http-XXXXXX",dir);
    if(!mkdtemp(tmp)) {free(wire);ns_reply(client,errno,0,0,0,0,NULL,0,NULL,0);return 0;}
    char body[280],out[280],head[280],err[280],at[282];
    snprintf(body,sizeof(body),"%s/body",tmp);snprintf(out,sizeof(out),"%s/out",tmp);
    snprintf(head,sizeof(head),"%s/headers",tmp);snprintf(err,sizeof(err),"%s/err",tmp);
    snprintf(at,sizeof(at),"@%s",body);
    int error=0,reset=0,timedout=0,status=0,fd=open(body,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
    if(fd<0 || ns_exact(fd,wire+nm+nu+nh,nb,1))error=errno?errno:EIO;
    if(fd>=0)close(fd);
    free(wire);
    char *args[100];unsigned count=0;
#define HTTP_ARG(s) args[count++]=(char *)(s)
    HTTP_ARG(curl);HTTP_ARG("-q");HTTP_ARG("--silent");HTTP_ARG("--show-error");
    HTTP_ARG("--proto");HTTP_ARG("=http,https");HTTP_ARG("--request");HTTP_ARG(method);
    HTTP_ARG("--url");HTTP_ARG(url);HTTP_ARG("--output");HTTP_ARG(out);
    HTTP_ARG("--dump-header");HTTP_ARG(head);
    if(!strcmp(method,"HEAD"))HTTP_ARG("--head");
    else if(nb || !strcmp(method,"POST") || !strcmp(method,"PUT") || !strcmp(method,"PATCH"))
        {HTTP_ARG("--data-binary");HTTP_ARG(at);}
    char *next=headers;unsigned hc=0;
    while(*next && !error) {
        char *end=strchr(next,'\n');if(end)*end=0;
        if(++hc>32 || !strchr(next,':') || strchr(next,'\r')) {error=EINVAL;break;}
        HTTP_ARG("--header");HTTP_ARG(next);if(!end)break;next=end+1;
    }
    args[count]=NULL;
#undef HTTP_ARG
    pid_t child=-1;
    if(!error) {
        pid_t parent=getpid();child=fork();
        if(!child) {
            ns_parent_guard(parent);setpgid(0,0);
            int log=open(err,O_WRONLY|O_CREAT|O_EXCL,0600),null=open("/dev/null",O_RDWR);
            if(log<0 || null<0)_exit(127);
            dup2(null,0);dup2(null,1);dup2(log,2);
            for(int f=3;f<1024;f++)close(f);
            struct rlimit size={NS_OUTPUT,NS_OUTPUT};if(setrlimit(RLIMIT_FSIZE,&size))_exit(127);
            execv(curl,args);_exit(127);
        }
        if(child<0)error=errno;else setpgid(child,child);
    }
    long deadline=ns_now()+timeout;
    while(child>0 && waitpid(child,&status,WNOHANG)==0) {
        if(ns_stopping || ns_now()>=deadline) {
            reset=ns_stopping?1:0;timedout=!ns_stopping;kill(-child,SIGKILL);break;
        }
        struct pollfd p={.fd=listener,.events=POLLIN};poll(&p,1,5);
        if(p.revents & POLLIN) {
            int other=ns_accept(listener);char h[256];ns_timeout(other,1000);
            if(other>=0 && !ns_line(other,h,sizeof(h))) {
                char sid[33],target[33];int cancel=sscanf(h,"CANCEL %32s %32s",sid,target)==2;
                if(!strncmp(h,"CLOSE ",6) || (cancel && !strcmp(target,operation))) {
                    ns_retire_session(id);
                    ns_stopping=1;reset=1;kill(-child,SIGKILL);
                    ns_reply(other,0,0,0,0,1,NULL,0,NULL,0);
                } else ns_reply(other,cancel?ENOENT:EBUSY,0,0,0,0,NULL,0,NULL,0);
            }
            if(other>=0)close(other);
        }
    }
    if(child>0) {ns_children(child);kill(-child,SIGKILL);while(waitpid(child,&status,0)<0 && errno==EINTR){}}
    char *payload=malloc(limit),response[8193]={0};size_t used=0,hs=0;int truncated=0,code=0;
    fd=open(head,O_RDONLY|O_CLOEXEC);if(fd>=0) {
        ssize_t n=read(fd,response,8192);if(n>0)hs=(size_t)n;
        struct stat st;if(!fstat(fd,&st) && st.st_size>(off_t)hs)truncated=1;
        close(fd);}
    for(char *line=response;line && *line;) {
        int candidate;if(sscanf(line,"HTTP/%*s %d",&candidate)==1)code=candidate;
        char *end=strchr(line,'\n');line=end?end+1:NULL;
    }
    fd=strcmp(method,"HEAD")?open(out,O_RDONLY|O_CLOEXEC):-1;
    if(fd>=0 && payload) {ssize_t n=read(fd,payload,limit);if(n>0)used=(size_t)n;struct stat st;
        if(!fstat(fd,&st) && st.st_size>(off_t)used)truncated=1;
        close(fd);}
    if(!code) {fd=open(err,O_RDONLY|O_CLOEXEC);if(fd>=0) {ssize_t n=read(fd,response,8192);hs=n>0?(size_t)n:0;close(fd);}}
    if(!payload && !error)error=ENOMEM;
    if(child>0 && !reset && !timedout && (!WIFEXITED(status) || WEXITSTATUS(status))) {
        error=EIO;
        int curl_code=WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
        hs=(size_t)snprintf(response,sizeof(response),"curl transport failed (exit %d, HTTP %d): ",curl_code,code);
        fd=open(err,O_RDONLY|O_CLOEXEC);
        if(fd>=0) {ssize_t n=read(fd,response+hs,8192-hs);if(n>0)hs+=(size_t)n;close(fd);}
    }
    ns_reply(client,error,reset?125:code,timedout,truncated,reset,payload,used,response,hs);
    free(payload);unlink(body);unlink(out);unlink(head);unlink(err);rmdir(tmp);
    return reset;
}
