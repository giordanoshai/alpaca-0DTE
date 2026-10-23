"""FastAPI 模型定义"""
from pydantic import BaseModel


class WatcherStatus(BaseModel):
    """盯盘状态"""
    running: bool
