myharem
=======

MySQL/MariaDB/Percona tool to install multiple instances on the same host, easy and quick.


INSTALLATION:

Copy all mh-* files under your bin directory.

Copy etc/myharem.conf under /etc and set basedir to your preferred location, user must have privileges.

Under the folder 'myharem/local' put the .tar.gz packages.


GENERAL INSTRUCTIONS:

Type 'mh' for a list of commands.

Type 'mh <command>' for the syntax.



EXAMPLES:

To install an instace just use:

$ mh deploy <package.tar.gz> <instance id>

E.g.:

$ mh deploy mariadb-10.1.14-linux-x86_64.tar.gz 10114

To install another instance just use:

$ mh deploy mariadb-10.1.14-linux-x86_64.tar.gz 20114


START AN INSTANCE:

$ mh service start 10114

STOP AN INSTANCE:

$ mh service stop 10014


STATUS OF INSTANCES:

$ mh service status
