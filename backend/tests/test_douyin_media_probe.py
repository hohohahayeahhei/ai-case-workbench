import importlib.util
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from backend.app.douyin_media import MAX_MEDIA_SECONDS, MediaError, probe_media, transcribe_local


class DouyinMediaProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "speech.wav"
        self.make_wav(2)

    def tearDown(self):
        self.temp.cleanup()

    def make_wav(self, seconds):
        # A valid low-sample-rate WAV keeps the 30-minute boundary fixture small.
        with wave.open(str(self.path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(1)
            output.writeframes(b"\x00\x00" * seconds)

    def test_no_probe_dependency_stops_before_transcription(self):
        with patch("backend.app.douyin_media.shutil.which", return_value=None), \
             patch("backend.app.douyin_media.importlib.util.find_spec", return_value=None), \
             patch("backend.app.douyin_media.local_capabilities", return_value={"transcription_available": True}), \
             patch("backend.app.douyin_media.subprocess.run") as run:
            with self.assertRaisesRegex(MediaError, "无法验证素材时长"):
                transcribe_local(self.path, "audio/wav")
            run.assert_not_called()

    def test_pyav_probe_has_timeout_and_rejects_invalid_duration(self):
        for duration in (None, 0, -1, "nan", "inf", MAX_MEDIA_SECONDS + 0.001):
            with self.subTest(duration=duration), \
                 patch("backend.app.douyin_media.shutil.which", return_value=None), \
                 patch("backend.app.douyin_media.importlib.util.find_spec", return_value=object()), \
                 patch("backend.app.douyin_media.subprocess.run", return_value=subprocess.CompletedProcess(
                     [], 0, stdout=json.dumps({"format": {"duration": duration}}).encode())) as run:
                with self.assertRaises(MediaError):
                    probe_media(self.path, timeout=3)
                self.assertEqual(run.call_args.kwargs["timeout"], 3)
                self.assertTrue(run.call_args.kwargs["check"])

    def test_probe_timeout_and_corruption_become_safe_errors(self):
        for failure in (subprocess.TimeoutExpired(["probe"], 1),
                        subprocess.CalledProcessError(1, ["probe"], stderr=b"private path")):
            with self.subTest(failure=type(failure).__name__), \
                 patch("backend.app.douyin_media.shutil.which", return_value=None), \
                 patch("backend.app.douyin_media.importlib.util.find_spec", return_value=object()), \
                 patch("backend.app.douyin_media.subprocess.run", side_effect=failure):
                with self.assertRaisesRegex(MediaError, "本地媒体解析失败") as error:
                    probe_media(self.path)
                self.assertNotIn("private path", str(error.exception))

    def test_playlist_disguised_as_video_is_rejected_before_probe(self):
        playlist = self.path.with_suffix(".mp4")
        playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:10\nhttps://127.0.0.1/private.ts\n")
        with patch("backend.app.douyin_media.subprocess.run") as run:
            with self.assertRaisesRegex(MediaError, "不支持的素材格式"):
                probe_media(playlist)
            run.assert_not_called()

    def test_symlink_and_nonlocal_input_cannot_reach_probe(self):
        link = self.path.with_name("link.wav")
        link.symlink_to(self.path)
        with patch("backend.app.douyin_media.subprocess.run") as run:
            for path in (link, Path("https://example.com/media.mp4")):
                with self.subTest(path=str(path)), self.assertRaises(MediaError):
                    probe_media(path)
            run.assert_not_called()

    def test_ffprobe_keeps_existing_cli_and_checks_duration(self):
        with patch("backend.app.douyin_media.shutil.which", return_value="/usr/bin/ffprobe"), \
             patch("backend.app.douyin_media.subprocess.run", return_value=subprocess.CompletedProcess(
                 [], 0, stdout=b'{"format":{"duration":"1800"},"streams":[{"codec_type":"audio"}]}')) as run:
            result = probe_media(self.path)
        self.assertEqual(result["duration_seconds"], MAX_MEDIA_SECONDS)
        self.assertEqual(result["probe_engine"], "ffprobe")
        self.assertEqual(run.call_args.args[0][:6],
                         ["/usr/bin/ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries"])

    @unittest.skipUnless(importlib.util.find_spec("av"), "optional PyAV not installed")
    def test_real_pyav_reads_local_wav_and_rejects_long_duration(self):
        with patch("backend.app.douyin_media.shutil.which", return_value=None):
            result = probe_media(self.path)
            self.assertEqual(result["duration_seconds"], 2)
            self.assertEqual(result["probe_engine"], "pyav")
            self.assertEqual(result["streams"], [{"codec_type": "audio"}])
            self.make_wav(MAX_MEDIA_SECONDS)
            self.assertEqual(probe_media(self.path)["duration_seconds"], MAX_MEDIA_SECONDS)
            self.make_wav(MAX_MEDIA_SECONDS + 1)
            with self.assertRaisesRegex(MediaError, "30 分钟"):
                probe_media(self.path)


if __name__ == "__main__":
    unittest.main()
