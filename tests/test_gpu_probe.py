import unittest
from unittest.mock import patch

from saymore import gpu_probe


class DetectDeviceTest(unittest.TestCase):
    """detect_device 的判定门槛:Vulkan 可用 且 独显专用显存 ≥4GB → cuda,否则 cpu。"""

    def _detect(self, vulkan, vram_gib):
        with patch.object(gpu_probe, "_vulkan_available", return_value=vulkan), \
             patch.object(gpu_probe, "max_dedicated_vram_bytes",
                          return_value=int(vram_gib * 1024 ** 3)):
            return gpu_probe.detect_device()

    def test_discrete_gpu_with_enough_vram_uses_cuda(self):
        self.assertEqual(self._detect(vulkan=True, vram_gib=8.0), "cuda")

    def test_nominal_4gb_card_reporting_slightly_under_still_cuda(self):
        # 4GB 卡实报常略低于 4GiB(系统预留),本机实测 3.84 GiB 也要收
        self.assertEqual(self._detect(vulkan=True, vram_gib=3.84), "cuda")

    def test_small_vram_falls_back_to_cpu(self):
        # 2GB 老卡 / 核显(专用显存仅几百 MB)→ CPU
        self.assertEqual(self._detect(vulkan=True, vram_gib=2.0), "cpu")
        self.assertEqual(self._detect(vulkan=True, vram_gib=0.25), "cpu")

    def test_no_vulkan_driver_forces_cpu(self):
        # 有大显存独显但装不上 Vulkan 驱动,本程序 GPU 后端用不了 → CPU
        self.assertEqual(self._detect(vulkan=False, vram_gib=12.0), "cpu")

    def test_probe_exception_falls_back_to_cpu(self):
        with patch.object(gpu_probe, "_vulkan_available", side_effect=RuntimeError("boom")):
            self.assertEqual(gpu_probe.detect_device(), "cpu")


if __name__ == "__main__":
    unittest.main()
