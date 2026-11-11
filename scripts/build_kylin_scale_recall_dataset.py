#!/usr/bin/env python3
"""Build a 1000-doc labeled retrieval set for contest-scale Recall@5.

This is a self-built evaluation corpus, not an official organizer dataset.
Design: keep the 12 diagnostic OS-Agent facts as sparse needles, add a small
set of unique-entity office/desktop facts, and fill the rest with
cross-topic distractors. Queries are paraphrases, not copied document text.
Near-duplicate latency-probe sentences are intentionally excluded.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "benchmarks/datasets/kylin_joint_retrieval.json"
OUT = ROOT / "benchmarks/datasets/kylin_scale_recall_1000.json"
DOC_COUNT = 1000

DESCRIPTION = (
    "自建千条知识检索标注集（非官方赛题集）。"
    "12条办公/开发目标知识沿用诊断集原文，另含20条带唯一实体的桌面运维知识；"
    "其余为跨主题干扰项，避免近重复灌水。"
    "查询均为自然语言改写，每条标注1-2个相关文档，相关文档在全库中稀疏。"
    "用于在约1000条库上测量 Recall@5≥85% 与检索端到端≤500ms。"
    "不得与 scripts/windows_scale_acceptance.py 的近重复延迟语料混用或混报。"
)

# Distinctive strings that must not leak into distractors, so labeled needles stay unique.
RESERVED_PHRASES = (
    "清华镜像",
    "authorized_keys",
    "conda create",
    "git stash",
    "数据透视表",
    "演讲者视图",
    "插入自动目录",
    "UTF-8重新导入",
    "会议纪要模板",
    "项目周报模板",
    "复现步骤、预期结果",
    "pip安装Python依赖",
    "Docker通过-p参数",
    "SSH免密登录",
)

BEACON_ANCHORS = (
    "WPS 2019",
    "E-44",
    "vboxnet7",
    "easylist-kylin",
    "Source Han Sans CN Heavy",
    "Ctrl+Super+Space",
    "office-archive",
    "campus-wg-08",
    "intranet.mokoa.local",
    "/dev/md3",
    "/mnt/usb-backup-k7",
    "tag-invoice",
    "B2-17",
    "58443",
    "27134/tcp",
    "office-scan-17",
    "obs-kylin-stream-9c",
    "318442",
    "Dell P2419H",
    "TIGA-B550",
)

BEACONS = (
    ("wps_pdf_embed", "WPS 2019 专业版将合同导出为 PDF 时，请在另存为中选择 PDF 并勾选嵌入字体，避免印刷厂缺字。",
     "sq01", "用 WPS 2019 出 PDF 给印刷厂，怎样避免字体缺失？"),
    ("lenovo_e44", "联想 LJ2200 打印机弹出卡纸代码 E-44 时，打开前盖取出介质，清洁进纸辊后再按恢复键。",
     "sq02", "办公打印出现 E-44，联想 LJ2200 该怎么处理？"),
    ("vboxnet7", "VirtualBox 需要主机与虚拟机互通时，添加 Host-Only 适配器 vboxnet7，再把客户机网卡接到该网段。",
     "sq03", "VirtualBox 里怎样用 vboxnet7 让宿主机访问虚拟机？"),
    ("easylist_kylin", "麒麟浏览器广告过滤请订阅 uBlock 列表 easylist-kylin，不要同时启用互相冲突的第三条规则集。",
     "sq04", "桌面浏览器怎样订阅 easylist-kylin 过滤广告？"),
    ("sourcehan_heavy", "思源黑体 Source Han Sans CN Heavy 应复制到用户目录 ~/.fonts 后执行 fc-cache -fv 才能被办公套件调用。",
     "sq05", "Source Han Sans CN Heavy 装到哪里才会在文档里出现？"),
    ("fcitx_super", "小企鹅输入法 fcitx 在麒麟桌面用 Ctrl+Super+Space 切换中英文，避免占用 Ctrl+Space 与 IDE 冲突。",
     "sq06", "fcitx 想换一套不跟编辑器打架的中英文快捷键，用哪个？"),
    ("synology_ds220", "群晖 DS220+ 上的共享文件夹 office-archive 用于存放已归档合同，权限仅行政组可写。",
     "sq07", "归档合同应该放到哪台 NAS 的哪个共享目录？"),
    ("wg08", "校园网远程接入请使用 WireGuard 配置名 campus-wg-08，导入后先开启杀毒软件的隧道放行。",
     "sq08", "外出时用哪份 WireGuard 配置连学校网？"),
    ("cert_mokoa", "内网证书域名 intranet.mokoa.local 将于 2026-11-18 过期，到期前必须向信息中心申请续签。",
     "sq09", "intranet.mokoa.local 的证书什么时候到期、该找谁续？"),
    ("mdadm_md3", "软件阵列 /dev/md3 若提示降级，先用 mdadm --detail 确认失效盘，再热替换同容量硬盘。",
     "sq10", "/dev/md3 显示降级了，第一步查什么？"),
    ("rsync_k7", "夜间增量备份使用 rsync 同步到 /mnt/usb-backup-k7，保留权限并排除缓存目录。",
     "sq11", "日常文件备份要 rsync 到哪条 U 盘挂载路径？"),
    ("tbird_invoice", "Thunderbird 过滤器 tag-invoice 会把主题含发票的邮件打标签并归档到财务文件夹。",
     "sq12", "怎样让 Thunderbird 自动给发票邮件打 tag-invoice？"),
    ("cal_b217", "日历把会议室 B2-17 的提醒提前量设为 27 分钟，方便预留投影测试时间。",
     "sq13", "B2-17 开会前提前多少分钟提醒，好去测投影？"),
    ("pcap_58443", "排查登录超时请用 Wireshark 捕获过滤 tcp.port == 58443，只保留该鉴权端口的往返包。",
     "sq14", "抓包时怎样只看 58443 端口上的登录流量？"),
    ("fw_27134", "开放内部工具端口时执行 firewall-cmd --add-port=27134/tcp --permanent 后 reload，不要改 SSH 端口。",
     "sq15", "怎样用 firewall-cmd 放行 27134/tcp？"),
    ("gimp_tiff17", "扫描件用 GIMP 导出 TIFF 并选择 LZW 压缩，文件名使用 office-scan-17 以便档案室检索。",
     "sq16", "GIMP 导出档案扫描件要用哪种压缩和文件名？"),
    ("obs_9c", "活动直播在 OBS 填入推流密钥 obs-kylin-stream-9c，码率不超过 3500 kbps 以免教室出口拥塞。",
     "sq17", "教室直播 OBS 应该填哪条推流密钥？"),
    ("sony_318442", "会议室蓝牙音箱 Sony SRS-XB13 的配对码是 318442，首次连接后勾选信任该设备。",
     "sq18", "Sony SRS-XB13 对不上蓝牙，配对码是多少？"),
    ("dell_p2419h", "Dell P2419H 做竖屏阅读时，在显示设置中旋转 90 度，并关闭自动旋转以免窗口跳动。",
     "sq19", "Dell P2419H 怎样固定成竖屏？"),
    ("tiga_csm", "TIGA-B550 主板安装麒麟桌面前，开机按 Del 进入 BIOS 并关闭 CSM，以便使用 UEFI 启动项。",
     "sq20", "TIGA-B550 装系统前 BIOS 里要关掉哪个兼容模块？"),
)

DISTRACTOR_TEMPLATES = (
    "会议室 {n} 的投影仪接好线后若仍无画面，请把输入源切到 HDMI-{hdmi} 并降低刷新率后再投屏。",
    "门禁卡 AC-{n:04d} 无法打开园区西门时，到行政前台为当天重新开通闸机权限即可。",
    "台式机电源型号 PSU-{n:04d} 若频繁重启，先更换同功率电源并检查插座接地是否良好。",
    "摄像头 CAM-{n:04d} 在会议软件中黑屏时，先关闭其他占用镜头的应用，再重新授权隐私权限。",
    "无线路由器节点 AP-{n:04d} 信号差，可将信道改为 {ch} 并远离微波炉等干扰源。",
    "耳机型号 HS-{n:04d} 只有一侧有声，先到声音设置里关闭单声道，再重新插拔 3.5mm 接口。",
    "扫描仪 SCN-{n:04d} 预览全黑时，揭开稿台盖确认玻璃无遮挡，并将色彩模式改为灰度试扫。",
    "U 盘盘符 ident-disk-{n:04d} 在文件管理器只读，请确认写保护开关未打开后再重新挂载。",
    "笔记本风扇 FAN-{n:04d} 高负载狂转，可在电源管理中改为均衡模式并清理进风口灰尘。",
    "扩展坞 DOCK-{n:04d} 外接键鼠无响应，先换 USB-C 线缆，再在内核日志中确认是否枚举成功。",
    "绘图板 TAB-{n:04d} 压感失效时，卸载旧驱动后重装厂商驱动，并将应用切到笔输入模式。",
    "录音笔 REC-{n:04d} 导入后找不到文件，检查存储格式是否为 WAV，并关闭自动关机以免中断写入。",
    "考勤机 ATT-{n:04d} 无法联网，核对其固定地址与交换机 VLAN，再重启人员同步服务后重新下发名单。",
    "大屏 SPLIT-{n:04d} 拼接缝错位，进入处理器菜单执行几何校正，保存为场景 office-{n}。",
    "对讲机 RADIO-{n:04d} 杂音大，将功率调低一档并避开电梯机房，使用信道 {ch} 作为备用。",
    "标签机 LBL-{n:04d} 打印发虚，更换原装碳带，并将浓度设为 {density} 后再校准纸张类型。",
)


def load_base():
    data = json.loads(BASE.read_text(encoding="utf-8"))
    return list(data["documents"]), list(data["queries"])


def build():
    documents, queries = load_base()
    reserved = list(RESERVED_PHRASES) + list(BEACON_ANCHORS)
    for doc_id, text, qid, qtext in BEACONS:
        documents.append({"id": doc_id, "text": text})
        queries.append({"id": qid, "text": qtext, "relevant_ids": [doc_id]})
    extra_needles = {row["text"] for row in documents}
    n_targets = len(documents)
    needed = DOC_COUNT - n_targets
    if needed <= 0:
        raise RuntimeError("Target documents already exceed the 1000-doc budget")
    index = 0
    while len(documents) < DOC_COUNT:
        template = DISTRACTOR_TEMPLATES[index % len(DISTRACTOR_TEMPLATES)]
        serial = 2000 + index
        text = template.format(
            n=serial,
            hdmi=1 + index % 4,
            ch=1 + (index * 3) % 13,
            density=3 + index % 5,
        )
        if any(phrase in text for phrase in reserved):
            raise RuntimeError("Distractor leaked a reserved needle phrase")
        if text in extra_needles:
            raise RuntimeError("Duplicate distractor text")
        extra_needles.add(text)
        documents.append({"id": f"dist-{index:04d}", "text": text})
        index += 1
    if len(documents) != DOC_COUNT:
        raise RuntimeError("Document count mismatch")
    ids = [row["id"] for row in documents]
    if len(set(ids)) != len(ids):
        raise RuntimeError("Duplicate document IDs")
    payload = {
        "description": DESCRIPTION,
        "contest_metrics": {
            "recall_at_k": 0.85,
            "latency_ms": 500,
            "k": 5,
            "documents": DOC_COUNT,
            "scope": "labeled retrieval; not HTTP latency probe",
        },
        "documents": documents,
        "queries": queries,
    }
    return payload


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    target = Path(argv[0]) if argv else OUT
    payload = build()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "path": str(target),
        "documents": len(payload["documents"]),
        "queries": len(payload["queries"]),
        "targets": 12 + len(BEACONS),
        "distractors": DOC_COUNT - 12 - len(BEACONS),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
