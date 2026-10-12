/* Bounded binary file operations shared by chronus executors. */
struct ns_upload {uint64_t total,next;unsigned mode;char owner[33];char path[4097];};
static void ns_upload_cleanup(const char *owner) {
    DIR *d=opendir(ns_root);if(!d)return;struct dirent *e;
    while((e=readdir(d))) {
        if(strlen(e->d_name)!=39 || strcmp(e->d_name+32,".upload"))continue;
        char path[512];snprintf(path,sizeof(path),"%s/%s",ns_root,e->d_name);
        struct ns_upload u;int fd=open(path,O_RDWR|O_NOFOLLOW|O_CLOEXEC);
        if(fd<0)continue;
        if(!flock(fd,LOCK_EX) && !ns_exact(fd,&u,sizeof(u),0) &&
            memchr(u.path,0,sizeof(u.path)) && memchr(u.owner,0,sizeof(u.owner)) &&
            (!owner || !strcmp(u.owner,owner))) {
            char *stage=NULL;
            if(asprintf(&stage,"%s.dsec-upload-%.32s",u.path,e->d_name)>=0)unlink(stage);
            free(stage);unlink(path);
        }
        close(fd);
    }
    closedir(d);
}
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
        u.total=total;u.mode=mode;strcpy(u.path,path);strcpy(u.owner,ns_session_id);
        if(ns_exact(meta,&u,sizeof(u),1) || fsync(meta)) {error=errno?errno:EIO;unlink(stage);unlink(mp);}
        goto finish;
    }
    meta=open(mp,O_RDWR|O_NOFOLLOW|O_CLOEXEC);
    if(meta<0) {error=errno;goto finish;}
    if(flock(meta,LOCK_EX) || ns_exact(meta,&u,sizeof(u),0)) {error=errno?errno:EIO;goto finish;}
    if(!memchr(u.owner,0,sizeof(u.owner)) || strcmp(u.owner,ns_session_id)) {error=EPERM;goto finish;}
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
