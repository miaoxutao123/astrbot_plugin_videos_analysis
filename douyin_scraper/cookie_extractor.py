#!/usr/bin/env python3
"""
抖音 Cookie 提取器 - 独立版本
可移植到其他项目的完整 Cookie 解析函数
"""



def extract_douyin_cookies(full_cookie_string: str) -> tuple[str, bool, dict[str, str]]:
    """
    从完整的 Cookie 字符串中提取抖音必需的 12 个字段

    Args:
        full_cookie_string: 从浏览器复制的完整 Cookie 字符串

    Returns:
        Tuple[str, bool, Dict[str, str]]:
            - 格式化的 Cookie 字符串（可直接用于代码）
            - Cookie 是否有效（包含所有关键字段）
            - 提取的 Cookie 字典（包含所有字段状态）
    """
    # 必需的 Cookie 字段
    required_cookies = [
        "odin_tt", "passport_fe_beating_status", "sid_guard", "uid_tt", "uid_tt_ss",
        "sid_tt", "sessionid", "sessionid_ss", "sid_ucp_v1", "ssid_ucp_v1",
        "passport_assist_user", "ttwid"
    ]

    # 关键字段（必须存在）
    critical_fields = ["sessionid", "uid_tt", "ttwid", "sid_guard"]

    # 解析 Cookie 字符串
    cookie_dict = {}
    if "=" in full_cookie_string:
        # 标准格式：name=value; name2=value2;
        pairs = full_cookie_string.replace(" ", "").split(";")
        for pair in pairs:
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookie_dict[name.strip()] = value.strip()

    # 提取需要的字段
    extracted = {}
    for cookie_name in required_cookies:
        extracted[cookie_name] = cookie_dict.get(cookie_name, "xxx")

    # 验证完整性
    missing_fields = []
    for field_name, field_value in extracted.items():
        if field_value == "xxx" or not field_value:
            missing_fields.append(field_name)

    # 检查关键字段
    critical_missing = [field for field in critical_fields if field in missing_fields]
    is_valid = len(critical_missing) == 0

    # 格式化为抖音下载器可用的 Cookie 字符串
    ordered_names = required_cookies  # 保持定义的顺序
    cookie_pairs = []
    for name in ordered_names:
        value = extracted.get(name, "xxx")
        cookie_pairs.append(f"{name}={value}")

    formatted_cookie = ";".join(cookie_pairs) + ";"

    return formatted_cookie, is_valid, extracted


# 简化版本 - 仅返回格式化的 Cookie
def extract_and_format_cookies(full_cookie_string: str) -> str:
    """
    简化版本：仅返回格式化的 Cookie 字符串

    Args:
        full_cookie_string: 从浏览器复制的完整 Cookie 字符串

    Returns:
        str: 格式化的 Cookie 字符串
    """
    formatted_cookie, _, _ = extract_douyin_cookies(full_cookie_string)
    return formatted_cookie


# 使用示例
if __name__ == "__main__":
    # 示例 Cookie 字符串（实际使用时替换为真实的 Cookie）
    example_cookie = """
    odin_tt=example_value1;
    passport_fe_beating_status=example_value2;
    sid_guard=example_value3;
    uid_tt=example_value4;
    uid_tt_ss=example_value5;
    sid_tt=example_value6;
    sessionid=example_value7;
    sessionid_ss=example_value8;
    sid_ucp_v1=example_value9;
    ssid_ucp_v1=example_value10;
    passport_assist_user=example_value11;
    ttwid=example_value12;
    """

    # 使用完整版本
    formatted, is_valid, extracted = extract_douyin_cookies(example_cookie)

    print("=== 抖音 Cookie 提取结果 ===")
    print(f"Cookie 是否有效: {'是' if is_valid else '否'}")
    print(f"\n格式化的 Cookie:\n{formatted}")

    print("\n提取的字段详情:")
    for name, value in extracted.items():
        status = "✓" if value != "xxx" and value else "✗"
        print(f"  {name}: {status}")

    # 使用简化版本
    print("\n=== 简化版本使用 ===")
    simple_formatted = extract_and_format_cookies(example_cookie)
    print(f"简化版本结果:\n{simple_formatted}")
