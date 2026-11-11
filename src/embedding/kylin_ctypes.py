"""ctypes bindings for Kylin CoreAI embedding C API (run on Kylin OS only)."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctypes import CDLL

# Header: <coreai/embedding/embedding.h>
# Install: sudo apt install libkylin-coreai-embedding-dev
LIB_NAME = "kysdk-coreai-embedding"
LIB_CANDIDATES = (
    "libkysdk-coreai-embedding.so",
    "libkylin-coreai-embedding.so",
)


class KylinEmbeddingLib:
    """
    Low-level ctypes wrapper. Owner: efca9634

    See docs/KYLIN_SDK.md for full API reference.
    """

    def __init__(self) -> None:
        if platform.system() != "Linux":
            raise OSError("Kylin embedding SDK is only available on Kylin Linux")
        self._lib = self._load()
        self._bind()

    def _load(self) -> CDLL:
        explicit = os.getenv("KYLIN_EMBEDDING_LIBRARY")
        candidates = [explicit] if explicit else []
        discovered = ctypes.util.find_library(LIB_NAME)
        if discovered:
            candidates.append(discovered)
        candidates.extend(LIB_CANDIDATES)

        errors: list[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return ctypes.CDLL(candidate)
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")
        detail = "; ".join(errors) or "未找到候选动态库"
        raise OSError(
            "无法加载银河麒麟 CoreAI Embedding SDK。"
            "请先安装 AI 子系统和 libkylin-coreai-embedding-dev，"
            "或设置 KYLIN_EMBEDDING_LIBRARY 指向实际 .so。"
            f" 尝试结果: {detail}"
        )

    def _bind(self) -> None:
        # Session
        self.create = self._lib.text_embedding_create_session
        self.create.restype = ctypes.c_void_p

        self.init = self._lib.text_embedding_init_session
        self.init.argtypes = [ctypes.c_void_p]
        self.init.restype = ctypes.c_int

        self.init_model = self._lib.text_embedding_init_model
        self.init_model.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.init_model.restype = ctypes.c_int

        self.get_model_list = self._lib.text_embedding_get_model_list
        self.get_model_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.get_model_list.restype = ctypes.c_void_p

        self.model_list_count = self._lib.embedding_model_list_get_count
        self.model_list_count.argtypes = [ctypes.c_void_p]
        self.model_list_count.restype = ctypes.c_int

        self.model_list_at = self._lib.embedding_model_list_get_model
        self.model_list_at.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.model_list_at.restype = ctypes.c_void_p

        self.model_info_name = self._lib.embedding_model_info_get_model_name
        self.model_info_name.argtypes = [ctypes.c_void_p]
        self.model_info_name.restype = ctypes.c_char_p

        self.model_info_dim = self._lib.embedding_model_info_get_model_dim
        self.model_info_dim.argtypes = [ctypes.c_void_p]
        self.model_info_dim.restype = ctypes.c_int

        self.destroy = self._lib.text_embedding_destroy_session
        self.destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.destroy.restype = None

        # Sync embed
        self.embed = self._lib.text_embedding
        self.embed.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self.embed.restype = ctypes.c_bool

        # Result
        self.get_vector = self._lib.embedding_result_get_vector_data
        self.get_vector.argtypes = [ctypes.c_void_p]
        self.get_vector.restype = ctypes.POINTER(ctypes.c_float)

        self.get_length = self._lib.embedding_result_get_vector_length
        self.get_length.argtypes = [ctypes.c_void_p]
        self.get_length.restype = ctypes.c_int

        self.get_error_code = self._lib.embedding_result_get_error_code
        self.get_error_code.argtypes = [ctypes.c_void_p]
        self.get_error_code.restype = ctypes.c_int

        self.get_error_message = self._lib.embedding_result_get_error_message
        self.get_error_message.argtypes = [ctypes.c_void_p]
        self.get_error_message.restype = ctypes.c_char_p

        self.result_destroy = self._lib.embedding_result_destroy
        self.result_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.result_destroy.restype = None

    def destroy_session(self, session: int) -> None:
        handle = ctypes.c_void_p(session)
        self.destroy(ctypes.byref(handle))

    def list_model_names(self, session: int) -> tuple[int, list[str]]:
        error = ctypes.c_int(-1)
        listing = self.get_model_list(session, ctypes.byref(error))
        if error.value != 0 or not listing:
            return error.value, []
        names: list[str] = []
        for index in range(max(0, self.model_list_count(listing))):
            info = self.model_list_at(listing, index)
            if not info:
                continue
            raw = self.model_info_name(info)
            if raw:
                names.append(raw.decode("utf-8", errors="replace"))
        return 0, names

    def embed_text(self, session: int, text: str) -> list[float]:
        result = ctypes.c_void_p()
        ok = self.embed(session, text.encode("utf-8"), ctypes.byref(result))
        if not ok:
            if result:
                code = self.get_error_code(result)
                raw_message = self.get_error_message(result)
                message = raw_message.decode("utf-8", errors="replace") if raw_message else ""
                self.result_destroy(ctypes.byref(result))
                raise RuntimeError(f"text_embedding failed: code={code}, message={message}")
            raise RuntimeError("text_embedding failed without result")
        if not result:
            raise RuntimeError("text_embedding returned a null result")
        try:
            length = self.get_length(result)
            ptr = self.get_vector(result)
            if length <= 0 or not ptr:
                raise RuntimeError("Kylin embedding returned an invalid vector")
            return [float(ptr[i]) for i in range(length)]
        finally:
            self.result_destroy(ctypes.byref(result))
