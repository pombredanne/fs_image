buck run //fs_image/rpm:snapshot-repos -- \
  --snapshot-dir=snapshot/centos8 \
  --gpg-key-whitelist-dir=config/centos8 \
  --db='{"kind": "sqlite", "db_path": "snapshot/db/snapshots.sql3"}' \
  --threads=16 \
  --storage='{"kind":"s3", "key": "s3", "bucket": "fs-image", "prefix": "centos8", "region": "us-west-2"}' \
  --one-universe-for-all-repos=centos8 \
  --dnf-conf=config/centos8/dnf.conf \
  --yum-conf=config/centos8/dnf.conf \
  --debug
