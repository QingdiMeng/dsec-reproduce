"""Explicit private leaf issuance, shared by initial deployment and Node enrollment."""
import secrets
import shutil
import subprocess
import ipaddress
import re


def openssl(*values):
    result = subprocess.run(['openssl', *map(str, values)], capture_output=True)
    if result.returncode:
        raise RuntimeError('Certificate generation failed; inspect the private output directory')


def certificate(ca, key, base, cn, hosts, usages):
    config = base.with_suffix('.cnf')
    def host_san(host):
        try:return 'IP:'+str(ipaddress.ip_address(host))
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',host):raise ValueError('Invalid certificate host')
            return 'DNS:'+host
    if not re.fullmatch(r'[A-Za-z0-9 ._-]{0,80}',cn):raise ValueError('Invalid certificate subject')
    if usages not in ('serverAuth','clientAuth','serverAuth,clientAuth'):raise ValueError('Explicit certificate role required')
    san = ','.join(host_san(host) for host in hosts)
    subject = 'CN='+cn if cn else 'O=DSec gateway client'
    config.write_text('[req]\nprompt=no\ndistinguished_name=dn\n[dn]\n'+subject+
        '\n[leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
        'extendedKeyUsage='+usages+'\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n'
        +('subjectAltName='+san+'\n' if san else ''))
    openssl('req','-new','-nodes','-newkey','rsa:2048','-keyout',base.with_suffix('.key'),
            '-out',base.with_suffix('.csr'),'-config',config)
    openssl('x509','-req','-in',base.with_suffix('.csr'),'-CA',ca,'-CAkey',key,
        '-set_serial','0x'+secrets.token_hex(12),'-days','30',
        '-out',base.with_suffix('.crt'),'-extfile',config,'-extensions','leaf')
    return base.with_suffix('.crt'), base.with_suffix('.key')


def authority(base,cn='DSec CA'):
    if not re.fullmatch(r'[A-Za-z0-9 ._-]{1,80}',cn):raise ValueError('Invalid CA subject')
    config=base.with_suffix('.cnf')
    config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ca\n'
        '[dn]\nCN='+cn+'\n[ca]\nbasicConstraints=critical,CA:TRUE\n'
        'keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n')
    openssl('req','-new','-x509','-nodes','-newkey','rsa:2048','-days','30',
            '-keyout',base.with_suffix('.key'),'-out',base.with_suffix('.crt'),'-config',config)
    base.with_suffix('.key').chmod(0o600)
    return base.with_suffix('.crt'),base.with_suffix('.key')


def copy_certificate(ca, cert, directory, name):
    for suffix, source in zip(('.crt','.key'), cert):
        shutil.copyfile(source, directory/(name+suffix))
        (directory/(name+suffix)).chmod(0o600)
    shutil.copyfile(ca, directory/'ca.crt')
