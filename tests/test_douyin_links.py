"""抖音链接识别与分享页播放地址升级。"""

import pytest

from astrbot_plugin_videos_analysis.douyin_scraper.douyin_parser import _DIRECT_ID, DOUYIN_LINK, _upgrade_share_item

VID = "7690812286114540025"


@pytest.mark.parametrize("text", [
    "看看 https://v.douyin.com/iRNBho6u/ 复制此链接",
    f"https://www.douyin.com/video/{VID}",
    f"https://www.douyin.com/note/{VID}?previous_page=app_code_link",
    f"https://www.iesdouyin.com/share/video/{VID}/?region=CN",
    f"https://www.douyin.com/jingxuan?modal_id={VID}",
    f"https://www.douyin.com/slides/{VID}",
])
def test_links_recognized(text):
    assert DOUYIN_LINK.search(text)


@pytest.mark.parametrize("text", ["https://www.douyin.com/", "https://www.bilibili.com/video/BV1GJ411x7h7", "douyin 很好玩"])
def test_non_work_links_ignored(text):
    assert not DOUYIN_LINK.search(text)


def test_direct_id_extracted():
    for url in (f"https://www.douyin.com/video/{VID}", f"https://www.douyin.com/user/x?modal_id={VID}",
                f"https://www.iesdouyin.com/share/note/{VID}/"):
        m = _DIRECT_ID.search(url)
        assert m and (m.group(1) or m.group(2)) == VID


def test_share_item_upgraded_to_unwatermarked_1080p():
    item = {"video": {"play_addr": {"url_list": [
        "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0x&ratio=720p&line=0",
        "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0x",
    ]}}}
    urls = _upgrade_share_item(item)["video"]["play_addr"]["url_list"]
    assert all("/play/" in u and "/playwm/" not in u for u in urls)
    assert urls[0].endswith("ratio=1080p&line=0") and urls[1].endswith("&ratio=1080p")
    assert _upgrade_share_item({"images": []}) == {"images": []}  # 图文没有 play_addr，原样返回
