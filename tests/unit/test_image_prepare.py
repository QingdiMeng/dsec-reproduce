"""Image preparation rejects drifting pins and insufficient space before writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dsec_image import prepare as builder


class ImagePrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        source = root / "input"
        source.write_text("fixture")
        self.args = dict(image="sha256:" + "a" * 64, tools_image="sha256:" + "b" * 64,
                         output=root / "output", environment_id="python-mbpp",
                         kernel=source, agent_source=source, busybox=source)
        self.inspect = dict(Id=self.args["image"], Architecture="amd64", Os="linux",
                            RootFS={"Layers": ["sha256:" + "c" * 64]}, Size=1024)

    def invoke(self, responses):
        with patch.object(builder.platform, "system", return_value="Linux"), \
             patch.object(builder.platform, "machine", return_value="x86_64"), \
             patch.object(builder.subprocess, "check_output", side_effect=responses):
            return builder.prepare(**self.args)

    def test_mutable_tag_rejected_without_docker_or_output(self):
        self.args["image"] = "python:latest"
        with self.assertRaisesRegex(ValueError, "exact local"):
            self.invoke([])
        self.assertFalse(self.args["output"].exists())

    def test_tools_identity_drift_rejected_before_conversion(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            self.invoke([json.dumps([self.inspect]), "sha256:" + "d" * 64])
        self.assertFalse(self.args["output"].exists())

    def test_low_disk_fails_before_export_or_creating_directory(self):
        from collections import namedtuple
        Usage = namedtuple("Usage", "total used free")
        with patch.object(builder.shutil, "disk_usage", return_value=Usage(100, 99, 1)), \
             self.assertRaisesRegex(RuntimeError, "disk reserve"):
            self.invoke([json.dumps([self.inspect]), self.args["tools_image"]])
        self.assertFalse(self.args["output"].exists())

    def test_drive_limit_fails_before_export(self):
        self.inspect["RootFS"]["Layers"] *= 13
        with self.assertRaisesRegex(ValueError, "1..12"):
            self.invoke([json.dumps([self.inspect]), self.args["tools_image"]])
        self.assertFalse(self.args["output"].exists())
