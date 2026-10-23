# seaweedfs-provisioner

A Kubernetes operator that creates SeaweedFS S3 buckets and bucket-limited access keys from `SeaweedfsBucket` and `SeaweedfsUser` resources, and writes each key into a Secret (`access_key`, `secret_key`, `endpoint`). It is the SeaweedFS sibling of [minio-provisioner](https://github.com/drewsonne/minio-provisioner).

## Install

```bash
helm install seaweedfs-provisioner chart/seaweedfs-provisioner \
  --namespace seaweedfs \
  --set seaweedfs.endpoint=seaweedfs-s3.seaweedfs.svc.cluster.local:8333 \
  --set seaweedfs.credentialsSecret.name=seaweedfs-admin
```

The admin key is read from a Secret in the release namespace (default `seaweedfs-admin`, keys `access_key` and `secret_key`; see `seaweedfs.credentialsSecret` in `values.yaml`). The image tag defaults to the chart's `appVersion`.
