"""
工具函数模块：文件发送和自动清理
"""

import asyncio
import os
import struct
import time

import aiofiles
import aiofiles.os

from astrbot.api import logger


async def send_file(filename: str, HOST: str, PORT: int) -> str | None:
    """通过 TCP 发送文件到远程 NAP 服务器，返回远端绝对路径"""
    writer = None
    try:
        reader, writer = await asyncio.open_connection(HOST, PORT)
        file_name = os.path.basename(filename)
        file_name_bytes = file_name.encode("utf-8")

        writer.write(struct.pack(">I", len(file_name_bytes)))
        writer.write(file_name_bytes)

        file_size = await aiofiles.os.stat(filename)
        writer.write(struct.pack(">Q", file_size.st_size))

        async with aiofiles.open(filename, "rb") as f:
            while True:
                data = await f.read(4096)
                if not data:
                    break
                writer.write(data)
        await writer.drain()
        logger.info(f"文件 {file_name} 发送成功")

        file_abs_path_len_data = await _recv_all(reader, 4)
        if not file_abs_path_len_data:
            logger.warning("无法接收文件绝对路径长度")
            return None
        file_abs_path_len = struct.unpack(">I", file_abs_path_len_data)[0]

        file_abs_path_data = await _recv_all(reader, file_abs_path_len)
        if not file_abs_path_data:
            logger.warning("无法接收文件绝对路径")
            return None
        file_abs_path = file_abs_path_data.decode("utf-8")
        logger.info(f"接收端文件绝对路径: {file_abs_path}")
        return file_abs_path
    except Exception as e:
        logger.error(f"传输失败: {e}")
        return None
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _recv_all(reader, n: int) -> bytearray | None:
    """读取指定字节数"""
    data = bytearray()
    while len(data) < n:
        packet = await reader.read(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return data


def delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    """删除指定文件夹中超过时间阈值的旧文件"""
    try:
        os.makedirs(folder_path, exist_ok=True)
        time_threshold_seconds = time_threshold_minutes * 60
        current_time = time.time()
        deleted_count = 0

        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)
            if os.path.isfile(file_path):
                try:
                    file_time = os.path.getmtime(file_path)
                    if current_time - file_time > time_threshold_seconds:
                        os.remove(file_path)
                        logger.info(f"已删除过期文件: {file_path}")
                        deleted_count += 1
                except OSError as e:
                    logger.error(f"删除文件失败 {file_path}: {e}")

        if deleted_count > 0:
            logger.info(f"清理完成，共删除 {deleted_count} 个过期文件")

        return deleted_count

    except Exception as e:
        logger.error(f"清理文件夹失败 {folder_path}: {e}")
        return 0
