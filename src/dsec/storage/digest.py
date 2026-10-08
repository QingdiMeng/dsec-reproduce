"""File digest primitive shared by runtime and development provisioning."""
import hashlib


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()
