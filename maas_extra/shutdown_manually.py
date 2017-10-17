# Shuts down the node via ssh by a given hostname.

import os
import sys
import syslog

if __name__ == "__main__":
    cmd = "ssh ubuntu@" + str(sys.argv[1]) + " sudo poweroff"
    syslog.syslog(cmd)
    os.system(cmd)
