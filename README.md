# seaweedfs-provisioner

A Kubernetes operator that creates SeaweedFS S3 buckets and bucket-limited access keys from `SeaweedfsBucket` and `SeaweedfsUser` resources, and writes each key into a Secret (`access_key`, `secret_key`, `endpoint`). It is the SeaweedFS sibling of [minio-provisioner](https://github.com/drewsonne/minio-provisioner).
