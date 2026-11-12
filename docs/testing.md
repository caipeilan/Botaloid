# 测试

在项目根目录运行，使用安装了项目依赖的 Python 环境。

## 本地自动检查

```sh
python -m unittest discover -s tests
node --test tests/test_web.mjs
```

Python 测试覆盖检索、原文出处核对、曲库快照发布、模型协议与管理接口。默认跳过需要实际模型的语义选句测试。

`tests/test_fallback.py` 覆盖首轮成功即停、无候选与选句否决后的换方向、失败记录传递、重复方向去重、全曲库保底优先于人工兜底、等待预算、接口错误终止、保底回复的会话语义，以及人工兜底全部近期使用过时仍允许接话。

聊天最多进行首轮、两次换方向和全曲库保底四轮检索。每轮最多一次规划与一次选句；全曲库保底也未选出合适台词时，才检索人工标记并增加一次选句，调用上限为九次。无候选的轮次省略选句，首轮成功即停止；网页端所有调用共用本轮等待预算。接唱已核验到曲末时正常结束。

## 实际模型语义检查

语义测试使用「模型设置」中已配置的接口，会产生实际模型调用及该接口对应的费用。测试素材为自写短句，不需要个人曲库。

Windows PowerShell：

```powershell
$env:BOTALOID_TEST_MODEL = '1'
python -m unittest discover -s tests -p test_model_behavior.py -v
Remove-Item Env:BOTALOID_TEST_MODEL
```

Linux / macOS：

```sh
BOTALOID_TEST_MODEL=1 python -m unittest discover -s tests -p test_model_behavior.py -v
```

`tests/fixtures/selection_cases.json` 包含八种选句场景：身份答句与同义反问、称呼答句与同义反问、描述对象颠倒、回应陪伴与索求陪伴、接受道歉与重复道歉、能表达安慰的反问、用歌词表示做不到，以及通过人工兜底表达不知道。

规划阶段的 `reply_goal` 描述本轮交流动作，拒绝、安抚、比喻或表示做不到均可成为回应。选句阶段用 `check` 核对原句含义，并用 `target` 和候选的 `subject` 标记需要描述及实际描述的对象。程序排除明确对象不一致的选句，省略主语的台词按上下文判断。上述信息用于内部判断；最终返回仍只引用候选原文。

保底成功时，返回对象包含 `fallback: true` 和 `requested_intent`，以区别于实际查找命中；`conditions` 保留原始查找条件。将这些字段随助手历史回传，可让模型理解上一条歌词只是保底接话。过程中的失败记录仅用于本轮换方向，最终回复一次性发送。

语义测试验证这些固定候选的选择结果，完整曲库中的候选召回与实际对话效果需要结合具体曲库检查。
