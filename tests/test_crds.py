"""Schema checks for the SeaweedfsBucket and SeaweedfsUser CRDs."""

from pathlib import Path

import pytest
import yaml

CRD_DIR = Path(__file__).parent.parent / "chart" / "seaweedfs-provisioner" / "crds"


def load(name: str) -> dict:
    return yaml.safe_load((CRD_DIR / name).read_text())


def version(crd: dict) -> dict:
    (v,) = crd["spec"]["versions"]
    return v


def spec_schema(crd: dict) -> dict:
    return version(crd)["schema"]["openAPIV3Schema"]["properties"]["spec"]


@pytest.fixture(
    params=[
        ("seaweedfsbuckets.yaml", "SeaweedfsBucket", "seaweedfsbuckets"),
        ("seaweedfsusers.yaml", "SeaweedfsUser", "seaweedfsusers"),
    ]
)
def crd(request: pytest.FixtureRequest) -> tuple[dict, str, str]:
    name, kind, plural = request.param
    c = load(name)
    return c, kind, plural


def test_common_shape(crd: tuple[dict, str, str]) -> None:
    c, kind, plural = crd
    assert c["spec"]["group"] == "seaweedfs.sonne.zone"
    assert c["spec"]["scope"] == "Namespaced"
    assert c["spec"]["names"]["kind"] == kind
    assert c["spec"]["names"]["plural"] == plural
    assert c["metadata"]["name"] == f"{plural}.seaweedfs.sonne.zone"
    v = version(c)
    assert v["name"] == "v1"
    assert v["served"]
    assert v["storage"]
    assert v["subresources"] == {"status": {}}
    ready = [p for p in v["additionalPrinterColumns"] if p["name"] == "Ready"]
    assert ready[0]["jsonPath"] == ".status.ready"


def test_bucket_versioning_defaults_false() -> None:
    versioning = spec_schema(load("seaweedfsbuckets.yaml"))["properties"]["versioning"]
    assert versioning["type"] == "boolean"
    assert versioning["default"] is False


def test_user_schema() -> None:
    crd = load("seaweedfsusers.yaml")
    spec = spec_schema(crd)
    props = spec["properties"]
    assert spec["required"] == ["secretName"]
    assert props["bucket"]["type"] == "string"
    assert props["buckets"]["type"] == "array"
    assert props["buckets"]["minItems"] == 1
    assert props["buckets"]["items"]["type"] == "string"
    assert props["access"]["enum"] == ["read-write", "read-only"]
    assert props["access"]["default"] == "read-write"
    assert "secretNamespace" in props
    assert "secretNamespace" not in spec["required"]
    rules = [r["rule"] for r in spec["x-kubernetes-validations"]]
    assert rules == ["has(self.bucket) != has(self.buckets)"]


def test_user_printer_columns() -> None:
    columns = {
        p["name"]
        for p in version(load("seaweedfsusers.yaml"))["additionalPrinterColumns"]
    }
    assert {"Buckets", "Access", "Ready"} <= columns
