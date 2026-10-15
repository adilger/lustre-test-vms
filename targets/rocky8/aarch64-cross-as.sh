#!/bin/bash
# Installed over /usr/aarch64-linux-gnu/bin/as, the assembler EPEL8's
# aarch64 cross gcc runs, with the real one beside it as as.real.
#
# GCC 12 starts every .s it writes with ".arch armv8-a", and a .arch
# directive overrides the assembler's -march.  The EL8 kernel enables
# its ARMv8.4 TLBI range instructions (rvale1is, rvae1is) only through
# -Wa,-march=armv8.4-a, which works with RHEL's own GCC 8 toolchain but
# not this one: mm/memory.o and arch/arm64/mm/hugetlbpage.o fail.  So
# when the command line names -march, drop GCC's default .arch line and
# let -march decide.  Without -march, everything passes through.

real=/usr/aarch64-linux-gnu/bin/as.real

march=false
for a in "$@"; do
	[[ $a == -march=* ]] && march=true
done
$march || exec "$real" "$@"

# GCC passes the .s last, or nothing with -pipe (stdin).
in=-
if (($# > 1)) && [[ ${!#} != -* && -f ${!#} && ${*: -2:1} != -o ]]; then
	in=${!#}
	set -- "${@:1:$#-1}"
fi

tmp=$(mktemp --suffix=.s) || exit 1
trap 'rm -f "$tmp"' EXIT
if [[ $in == - ]]; then
	sed '/^\t\.arch armv8-a$/d' > "$tmp"
else
	sed '/^\t\.arch armv8-a$/d' "$in" > "$tmp"
fi || exit 1
"$real" "$@" "$tmp"
