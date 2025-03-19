import os
import time

# 设置时间阈值（单位：秒）
TIME_THRESHOLD = 60

# 指定文件夹路径
FOLDER_PATH = "D:/coding/code/python/astrbotplg/AstrBot/data/plugins/astrbot_plugin_videos_analysis/download_videos/dy"

def delete_old_files(folder_path, time_threshold):
    current_time = time.time()
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if os.path.isfile(file_path):
            # 获取文件的创建时间或最后修改时间
            file_time = os.path.getmtime(file_path)
            # 如果文件时间距当前时间大于阈值，删除文件
            if current_time - file_time > time_threshold:
                try:
                    os.remove(file_path)
                    print(f"Deleted: {file_path}")
                except Exception as e:
                    print(f"Error deleting {file_path}: {e}")

if __name__ == "__main__":
    delete_old_files(FOLDER_PATH, TIME_THRESHOLD)