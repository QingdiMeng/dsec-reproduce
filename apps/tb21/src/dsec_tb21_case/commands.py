"""Historical task command normalization; journal digests retain these bytes."""


def execution_command(command):
    return ("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:"
            "/usr/bin:/sbin:/bin; " + command)
