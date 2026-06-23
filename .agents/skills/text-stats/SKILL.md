---
name: text-stats
description: 统计文本的字符数/词数/行数/段落数/中文字符比例。当用户要求"统计这段文字"、"看看字数"、"文本分析"、"算字数"时使用。脚本依赖 Python stdlib,无外部依赖。
---

# 文本统计

## 使用流程

1. 用户给文本时,把文本作为 stdin 传给 `scripts/count.py`,或用 `--text "..."` 参数。
2. 调 `run_skill_script("text-stats", "count.py", script_args=["--text", "<用户文本>"])`。
3. 脚本输出 JSON,你把 JSON 解析后用自然语言总结给用户。

## 脚本用法

```
python scripts/count.py --help
python scripts/count.py --text "要统计的文本"
```

输出 JSON 含字段:
- `chars`: 总字符数(含空格)
- `chars_no_space`: 不含空格字符数
- `words`: 英文/数字词数(按空白切分)
- `lines`: 行数
- `paragraphs`: 段落数(空行分隔)
- `chinese_chars`: 中文字符数
- `chinese_ratio`: 中文字符占比

## 注意

- 文本含特殊字符或换行,务必用双引号包裹传给 `--text`。
- 文本超长(>30KB)走 stdin 而不是 --text(命令行参数有长度限制)。MVP 暂只支持 --text。
- 把 JSON 转成中文总结(如"共 256 字符,其中中文 80%,3 个段落")给用户,不要直接贴 JSON。
