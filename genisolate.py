base = """# This is a configuration file for Isolate

# All sandboxes are created under this directory.
# To avoid symlink attacks, this directory and all its ancestors
# must be writeable only to root.
box_root = /var/local/lib/isolate

# Directory where lock files are created.
lock_root = /run/isolate/locks

# Control group under which we place our subgroups
# Either an explicit path to a subdirectory in cgroupfs, or "auto:file" to read
# the path from "file", where it is put by isolate-cg-helper.
# cg_root = /sys/fs/cgroup/isolate.slice/isolate.service
cg_root = auto:/run/isolate/cgroup

# Block of UIDs and GIDs reserved for sandboxes
first_uid = 60000
first_gid = 60000
num_boxes = 5050

# Only root can create new sandboxes (default: 0=everybody can)
#restricted_init = 1

# Per-box settings of the set of allowed CPUs and NUMA nodes
# (see linux/Documentation/cgroups/cpusets.txt for precise syntax)

#box0.cpus = 4-7
#box0.mems = 1"""

for i in range(5030):
    base += f"\nbox{i}.cpus = {i%8}"

with open("isolate", "w") as f:
    f.write(base + "\n")