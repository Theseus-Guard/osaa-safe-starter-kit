#!/bin/sh
# Deny-egress check for the demo. Run it inside the API container, which is attached to the internal backend network
# only. From the host, with the demo stack running:
#   docker compose -p safe-demo -f compose.yaml -f compose.demo.yaml exec -T api sh -s < scripts/demo-egress-check.sh
#
# It tries outbound connections that must all fail: a public DNS name (name resolution), a public IPv4 address and a
# public IPv6 address (TCP port 443). A positive control runs first: the internal service name "db" must resolve, which
# shows that the probe itself works, so a failure of the outbound probes means "denied", not "broken".
#
# Output: one line per check, then "PASS" and exit 0 when every check has the expected result. Any unexpected result
# prints "FAIL" and exits 1. The script sends no credential and writes nothing into the container.
#
# This check needs a running stack. The test suite does not run it; backend/tests/test_demo_egress.py checks the
# compose file that makes the result possible.
set -u

# Connects to HOST:443 (IPv4 or IPv6), or resolves NAME, and exits 0 only when that succeeds within 5 seconds.
PROBE='
import socket, sys
mode, target = sys.argv[1], sys.argv[2]
socket.setdefaulttimeout(5)
try:
    if mode == "dns":
        socket.getaddrinfo(target, 443)
    else:
        family = socket.AF_INET6 if mode == "ipv6" else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.connect((target, 443))
        sock.close()
except OSError:
    sys.exit(1)
sys.exit(0)
'

failures=0

# expect is "reachable" or "refused"; the probe exit status 0 means reachable.
check() {
  label="$1"
  expect="$2"
  mode="$3"
  target="$4"
  if python -c "$PROBE" "$mode" "$target" >/dev/null 2>&1; then
    result="reachable"
  else
    result="refused"
  fi
  if [ "$result" = "$expect" ]; then
    echo "ok   $label: $target is $result, as expected"
  else
    echo "FAIL $label: $target is $result, expected $expect"
    failures=$((failures + 1))
  fi
}

check "control" reachable dns db
check "dns" refused dns example.com
check "ipv4" refused ipv4 1.1.1.1
check "ipv6" refused ipv6 2606:4700:4700::1111

if [ "$failures" -ne 0 ]; then
  echo "FAIL $failures check(s) did not match the expected result"
  exit 1
fi
echo "PASS the demo reaches no public DNS name, IPv4 address or IPv6 address, and the internal control resolves"
exit 0
