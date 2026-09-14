"""Pull private_key_pk8 out of a Tiger .properties file into a .pem.

Usage:
    python extract_key.py <path to .properties> <path to write .pem>

Writes RAW BASE64 with no header lines, which is what read_private_key()
expects -- it strips only "-----BEGIN RSA PRIVATE KEY-----" markers, so a
PKCS#8 header would survive and break signing.
"""
import sys, pathlib

if len(sys.argv) != 3:
    print(__doc__)
    raise SystemExit(1)

source, target = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

value = None
for line in source.read_text(encoding="utf-8").splitlines():
    if line.startswith("private_key_pk8="):
        value = line.split("=", 1)[1].strip()
        break

if not value:
    raise SystemExit(f"No private_key_pk8= line found in {source}")

# Strip header lines if the value happens to carry them.
value = value.replace("-----BEGIN PRIVATE KEY-----", "")
value = value.replace("-----END PRIVATE KEY-----", "").strip()

target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(value + "\n", encoding="utf-8")
print(f"Wrote {len(value)} characters to {target}")

# Also print the OTHER values you need, so they can be pasted into .env.
print("\nPut these in .env:")
for line in source.read_text(encoding="utf-8").splitlines():
    for key, env in (("tiger_id=", "TIGER_ID"),
                     ("account=", "TIGER_ACCOUNT / TIGER_PAPER_ACCOUNT"),
                     ("license=", "TIGER_LICENSE")):
        if line.startswith(key):
            print(f"  {env:35} {line.split('=', 1)[1].strip()}")
