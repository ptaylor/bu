"""S3-compatible backend — stores files in an S3 bucket (or S3-compatible service).

Supports AWS S3, Backblaze B2, MinIO, and any other S3-compatible API.

Configuration keys:
    bucket: str — S3 bucket name (required)
    prefix: str — key prefix (folder) within the bucket (optional)
    region: str — AWS region or S3-compatible region (optional)
    endpoint_url: str — custom endpoint for S3-compatible services (optional)
    access_key: str — access key ID (optional; falls back to env/IMDS)
    secret_key: str — secret access key (optional; falls back to env/IMDS)

Environment variables also supported: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
AWS_DEFAULT_REGION, and B2-specific vars.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from bu.backends.base import Backend


class S3Backend(Backend):
    """Back up to an S3 or S3-compatible object store."""

    def _client(self):
        """Create and return a boto3 S3 client."""
        kwargs: dict[str, Any] = {}

        if endpoint_url := self.config.get("endpoint_url"):
            kwargs["endpoint_url"] = endpoint_url
        if region := self.config.get("region"):
            kwargs["region_name"] = region
        if access_key := self.config.get("access_key"):
            kwargs["aws_access_key_id"] = access_key
        if secret_key := self.config.get("secret_key"):
            kwargs["aws_secret_access_key"] = secret_key

        return boto3.client("s3", **kwargs)

    def _bucket(self) -> str:
        bucket = self.config.get("bucket", "")
        if not bucket:
            raise ValueError("S3 backend requires 'bucket' in config")
        return bucket

    def _prefix(self) -> str:
        return self.config.get("prefix", "").rstrip("/")

    def _key(self, rel_path: str) -> str:
        """Build the full S3 object key for a relative file path."""
        prefix = self._prefix()
        if prefix:
            return f"{prefix}/{rel_path}"
        return rel_path

    def _upload_file(self, local_path: Path, rel_path: str, dry_run: bool) -> dict[str, Any]:
        """Upload a single file to S3. Returns per-file stats."""
        size = local_path.stat().st_size
        if dry_run:
            return {"copied": 1, "skipped": 0, "bytes": size, "errors": []}

        try:
            client = self._client()
            key = self._key(rel_path)
            client.upload_file(str(local_path), self._bucket(), key)
            return {"copied": 1, "skipped": 0, "bytes": size, "errors": []}
        except (BotoCoreError, ClientError) as e:
            return {"copied": 0, "skipped": 0, "bytes": 0, "errors": [str(e)]}

    def _list_objects(self) -> list[dict[str, Any]]:
        """List all objects under the configured prefix."""
        client = self._client()
        bucket = self._bucket()
        prefix = self._prefix()
        objects: list[dict[str, Any]] = []

        paginator = client.get_paginator("list_objects_v2")
        kwargs = {"Bucket": bucket}
        if prefix:
            kwargs["Prefix"] = f"{prefix}/" if prefix else ""

        try:
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    objects.append(obj)
        except (BotoCoreError, ClientError):
            pass

        return objects

    # --- Backend interface ---

    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        files_copied = 0
        files_skipped = 0
        bytes_copied = 0
        errors: list[str] = []

        # Build set of existing remote keys for skip detection
        if not dry_run:
            try:
                existing = {obj["Key"]: obj for obj in self._list_objects()}
            except (BotoCoreError, ClientError) as e:
                existing = {}
                errors.append(f"Failed to list remote objects: {e}")
        else:
            existing = {}

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                errors.append(f"Source not found: {src}")
                continue

            if src.is_file():
                rel = src.name
                key = self._key(rel)
                # Check if upload needed
                obj = existing.get(key)
                if obj and obj["Size"] == src.stat().st_size:
                    files_skipped += 1
                    continue
                result = self._upload_file(src, rel, dry_run)
            else:
                result = {"copied": 0, "skipped": 0, "bytes": 0, "errors": []}
                for root, _, files in os.walk(src):
                    for fname in files:
                        sf = Path(root) / fname
                        rel = str(sf.relative_to(src))
                        key = self._key(rel)
                        obj = existing.get(key)
                        if obj and obj["Size"] == sf.stat().st_size:
                            result["skipped"] += 1
                            continue
                        fr = self._upload_file(sf, rel, dry_run)
                        result["copied"] += fr["copied"]
                        result["skipped"] += fr["skipped"]
                        result["bytes"] += fr["bytes"]
                        result["errors"].extend(fr["errors"])

            files_copied += result["copied"]
            files_skipped += result["skipped"]
            bytes_copied += result["bytes"]
            errors.extend(result["errors"])

        return {
            "files_copied": files_copied,
            "files_skipped": files_skipped,
            "bytes_copied": bytes_copied,
            "errors": errors,
        }

    def restore(
        self,
        restore_path: str,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        restore = Path(restore_path).expanduser().resolve()
        files_restored = 0
        bytes_restored = 0
        errors: list[str] = []

        try:
            objects = self._list_objects()
        except (BotoCoreError, ClientError) as e:
            return {"files_restored": 0, "bytes_restored": 0, "errors": [str(e)]}

        prefix = self._prefix()
        prefix_len = len(prefix) + 1 if prefix else 0

        for obj in objects:
            key = obj["Key"]
            # Strip the prefix to get the relative path
            rel = key[prefix_len:] if prefix_len else key
            if not rel:
                continue

            dest_file = restore / rel
            if dry_run:
                files_restored += 1
                bytes_restored += obj["Size"]
                continue

            try:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                client = self._client()
                client.download_file(self._bucket(), key, str(dest_file))
                files_restored += 1
                bytes_restored += obj["Size"]
            except (BotoCoreError, ClientError, OSError) as e:
                errors.append(f"Failed to restore {key}: {e}")

        return {
            "files_restored": files_restored,
            "bytes_restored": bytes_restored,
            "errors": errors,
        }

    def check(
        self,
        source_paths: list[str],
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Get list of remote objects (key -> size mapping)
        try:
            existing = {obj["Key"]: obj["Size"] for obj in self._list_objects()}
        except (BotoCoreError, ClientError) as e:
            return {"files_to_backup": 0, "files_to_update": 0, "total_size": 0, "errors": [str(e)]}

        to_backup: list[str] = []
        total_size = 0

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                continue

            if src.is_file():
                key = self._key(src.name)
                if key not in existing or existing[key] != src.stat().st_size:
                    to_backup.append(str(src))
                    total_size += src.stat().st_size
            else:
                for root, _, files in os.walk(src):
                    for fname in files:
                        sf = Path(root) / fname
                        rel = str(sf.relative_to(src))
                        key = self._key(rel)
                        if key not in existing or existing[key] != sf.stat().st_size:
                            to_backup.append(str(sf))
                            total_size += sf.stat().st_size

        return {
            "files_to_backup": len(to_backup),
            "files_to_update": 0,
            "total_size": total_size,
            "files": to_backup,
        }

    def verify(
        self,
        source_paths: list[str],
        *,
        full: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            existing = {obj["Key"]: obj["Size"] for obj in self._list_objects()}
        except (BotoCoreError, ClientError) as e:
            return {"verified": 0, "missing": [], "mismatched": [], "errors": [str(e)]}

        verified = 0
        missing: list[str] = []
        mismatched: list[str] = []
        errors: list[str] = []

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                missing.append(str(src))
                continue

            if src.is_file():
                key = self._key(src.name)
                if key not in existing:
                    missing.append(str(src))
                elif existing[key] != src.stat().st_size:
                    mismatched.append(str(src))
                else:
                    verified += 1
            else:
                for root, _, files in os.walk(src):
                    for fname in files:
                        sf = Path(root) / fname
                        rel = str(sf.relative_to(src))
                        key = self._key(rel)
                        if key not in existing:
                            missing.append(str(sf))
                        elif existing[key] != sf.stat().st_size:
                            mismatched.append(str(sf))
                        else:
                            verified += 1

        return {
            "verified": verified,
            "missing": missing,
            "mismatched": mismatched,
            "errors": errors,
        }

    def status(
        self,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            objects = self._list_objects()
        except (BotoCoreError, ClientError) as e:
            return {"exists": False, "file_count": 0, "total_size": 0, "last_backup": None, "errors": [str(e)]}

        file_count = len(objects)
        total_size = sum(obj["Size"] for obj in objects)
        latest = max((obj["LastModified"] for obj in objects), default=None)

        return {
            "exists": True,
            "file_count": file_count,
            "total_size": total_size,
            "last_backup": latest.isoformat() if latest else None,
        }
