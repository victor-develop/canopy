# canopy

把发散的 Slack 讨论整理成一棵能导航的**子问题树**,每个节点带一条自己的
**checkpoint feed**。一个 skill,靠 `cron` 加一个无头 CLI 离线跑(默认 `codex exec`,
`claude -p` 也支持),没有常驻进程。

## 要解决什么

Slack 里的活是按树长的。A君在一个 thread 里聊问题 1,聊到一半冒出子问题 1.a,它有自己
的 owner、自己的 sub-thread、拉进来的是另一批人,结论再回灌给问题 1。这样能一直嵌套
下去(1.a.i、1.a.ii……),但 Slack 只有 channel 加一层 thread。三个痛点:

1. **嵌不下去** —— 树是真实存在的,Slack 表达不了。
2. **旁观者被淹** —— 关心这件事的人要的是关键进展,不是原始消息流。
3. **兜底的人没法导航** —— A君要把每个子节点、子子节点都推到收口,手上却没有一个
   能横着看全树的界面。

Canopy 把这棵树维护在旁边:盯每个活跃节点的新消息,消息里 `@` 了 agent 就派一个无头
CLI worker 去处理,给每个节点维护一条 checkpoint feed,再在频道里维护一条可点的
树消息。

### 一个人用也成立

树里不一定要有别人。把 Slack 当自己的工作台:一条 thread 一个问题,想到的子问题
`@canopy fork` 出去各自成 thread,feed 就是这条线的进度条,树消息是回来接着干时的
入口。

区别只在旁观者是谁 —— 团队场景里是等结论的人,个人场景里是三天后的自己。同时压着五六
件事、每件隔几天才回来一次的时候,读 feed 比把 thread 从头翻一遍快:feed 里只有决定、
结果、卡在哪,聊过程那些 summarizer 已经扔掉了。

## 怎么跑的

没有 daemon。状态全在磁盘上,默认放 `$CANOPY_DATA_HOME`(`~/.canopy/`)。cron 每 N
分钟醒一次,先跑一道**不花 LLM 的闸门**:每个活跃节点问一次 Slack 的最新 ts,没有新
消息就跳过。确实有活干才花 token —— 新消息里 `@` 了 agent 就起**完整 agent worker**,
没有就起**轻量 summarizer**,只更新 feed。worker 从磁盘冷启动,干完活推进 `cursor`、
释放节点锁、退出。中途崩了也不丢:下一 tick 从 `cursor` 重新拉。

跑 worker 的是哪个 CLI,由 `config.json` 的 `runner` 决定,默认 `codex`:

```jsonc
{ "runner": "codex" }                                // codex exec,默认
{ "runner": "claude" }                               // claude -p
{ "runner": { "cmd": ["my-wrapper", "--flag"] } }    // 自己包一层
```

两个都是 prompt 走 stdin、结果走 stdout、跑完退出,所以换 runner 只改一行配置。cron
的 `PATH` 很干净,`codex` 这类装在 mise / nvm 底下的二进制它看不见,所以 `track` 会先
把 runner 解析成绝对路径存进 `config.json`,解析不到就拒绝注册 cron —— 免得树看着在被
盯,其实一次都没 tick 过。

worker 跑在**无 sandbox、无审批**模式下:

```
codex   codex exec --dangerously-bypass-approvals-and-sandbox …
claude  claude -p --dangerously-skip-permissions …
```

cron 叫醒的进程没有 TTY,弹审批就是卡死;sandbox 一挡网络、或挡住节点目录外的写,
worker 就既发不出 Slack 也推不动自己的 `cursor`,而且不报错。代价说清楚:模型在这台
机器上的权限跟装 Canopy 的人一样大,而触发它的是别人在 Slack thread 里打的字。要隔离,
就用 `cmd` 那条口子把 runner 包进容器 / 独立账号 / 另一台机器。

完整设计看 [`SKILL.md`](./SKILL.md):三个 loop、两层 cron、runner、代码与数据分离、
命令集、状态 schema。

## 依赖的 slackcli 版本

所有 Slack 调用都走 [`slackcli`](https://github.com/shaharia-lab/slackcli),
用上游的就行 —— canopy 依赖的两个修复都已经合进上游 `main`:

| 修复 | 上游 PR | 落在哪个版本 |
|---|---|---|
| `chat.update` 传 `parse=none`,编辑不再吃掉链接 | [#120](https://github.com/shaharia-lab/slackcli/pull/120) | v0.9.0 **之后**,第一个带它的 release 还没发 |
| `slackcli update` 装之前校验 sha256 + 私有临时目录 | [#119](https://github.com/shaharia-lab/slackcli/pull/119) | 同上 |

所以现在有两条路:上游 `main`(`6c0a885` 或更新)自己 build,或者继续用
[victor-develop/slackcli](https://github.com/victor-develop/slackcli/releases)
的 **0.8.0-canopy.1**。等上游发了下一个 release,直接用那个 release 就够了 ——
**v0.9.0 及更早的版本不带 `parse=none`**,别按版本号大就以为有。

为什么在意:上游 v0.9.0 调 `chat.update` 时没传 `parse=none`,Slack 会把文本转义,
`<url|label>` 存成 `&lt;url|label&gt;`。feed 每追一条 checkpoint 就编辑一次那条
消息,所以在没打补丁的 CLI 上,第一条 checkpoint 之后 feed 里的链接全变成字面文字。

**这件事你不用自己判断。** `canopy track` 第一次跑的时候会拿自己的问题全景图那条消息
做一次实测:带着 `<url|label>` 编辑一遍,再把 Slack 存下来的文本读回来,看有没有被转义,
然后把结论写进 `config.json` 的 `slack_cli_escapes_on_edit`,之后的 track 直接用这个
答案。为什么不看版本号:上游 `main` 的 build 也自称 `0.9.0`,版本号分不出来。

被判定成会转义时,canopy 把带标签的链接降级成 `标签 URL`(还能点,标签变普通文字),
不会留一串 `&lt;…&gt;`。这个值你也可以手动写死,写了 canopy 就不再实测。

## 命令

本地 CLI(`/canopy …`):`track`、`agents`、`messages`、`tree`/`status`、
`recalibrate`、`map`、`untrack`。

Thread 里(`@<agent> …`):`fork`、`return`、`ack return`、`guide:`、`recalibrate`、
`untrack`。没有 `done` —— 完成态要配 reopen,还要定义「子节点还在跑算不算完成」。
Canopy 只做一件事:盯。所以只切换盯不盯。

## 完整流程:一个问题从盯到收

拿 `#pay` 一条「支付超时」的 thread 举例,从「这 thread 太长了」走到「整棵树收掉」。
图里右边标的,是这一步往 Slack 发了什么、用哪个模板。

### 0 · 每台机器配一次(可跳过)

```
$ /canopy agents                     加自己的 agent:profiles/arch.md、qa.md
                                       内置的 canopy 已经在了,不加也能跑
$ /canopy messages                   列出所有模板,以及各自从哪一层解析到的
                                       feed-root.md        user   (改过)
                                       track-announce.md   shipped
                                       ...
$ /canopy messages feed-root --preview
                                     渲染出 Slack 会收到的原文。不发。
```

装完就自带一个 agent:`canopy`。节点没设 `reply_as` 就用它回话,所以第一条 `track` 完
马上能 `@canopy fork …`,不用先写 profile。文件名就是句柄 —— 放一个
`profiles/arch.md` 进去,thread 里就能 `@arch`,再用节点的 `reply_as` 指过去。

### 1 · `track`:接管一条正在吵的 thread

```
$ /canopy track https://…/archives/C0PAY/p1699000001     # locale 默认 zh

  #pay ────────────────────────────────────────────────────────────────
   🧵 1699.0001  “支付超时”                    原始讨论,一个字不动
      └ [canopy]: 正在[跟踪]对话并进行 [智能总结]  track-announce.md
   📌 1699.0002  <支付超时> update feed:         feed-root.md
   🗺 1699.0003  <支付超时> trace tree · `pay-timeout`  tree-map.md
  ──────────────────────────────────────────────────────────────────────
   + 注册 cron          + ~/.canopy/projects/pay-timeout/tree.json
   projId 是 agent 起的语义化 short-id,不是标题的 slug
```

往 thread 里 announce 这一步不是客套:少了它,feed 建起来了,但正在那条 thread 里吵的人
不知道有这回事,A君只能挨个手动贴链接。它同时是 `fork` 和 `guide:` 的入口提示 —— 除了
A君,别人就是从这条消息知道有这些命令。

盯英文 thread 就加 `--locale en`。locale 只管 Canopy 自己发的那层框架文案,checkpoint
摘要是 summarizer 写的,thread 说什么语言它就跟着什么语言。

### 2 · 每 N 分钟一次 tick,平时没人看见

```
cron ──► 遍历每个 active 节点
           │
           ├ latest_ts <= cursor ? ──是──► 跳过                     0 token
           ├ 有 lock 文件 ?        ──是──► 跳过,下一 tick 再来
           │
           └ 新消息里 @ 了 agent ?
                ├ 否 ──────► 轻量 summarizer ──► 够格就往当前 feed 段
                │                                追一条 feed-entry.md
                ├ 是,且是命令 ► 直接跑代码 ──► fork / untrack / guide: …
                │                                (结构性改动不经过模型)
                └ 是,是问句 ─► 完整 worker ──► 在 thread 里回 reply.md
                                                 推进 cursor,释放 lock
```

### 3 · `guide:`:改它记什么

```
🧵 1  @canopy guide: 只记 DB 侧结论,排期讨论跳过
      → 追加到这个节点的 guide.md,下一 tick 生效
      → 只回一个 ✅ 表情,不发消息 —— thread 是给人读的
```

### 4 · `fork`:子问题分出去,自带 owner

```
🧵 1  @canopy fork 慢查询定位

  #pay ────────────────────────────────────────────────────────────────
   🧵 1699.0001  “支付超时”
      └ [canopy]: 拆出 `1.a` [慢查询定位]…      fork-announce.md
   🧵 1701.0500  “慢查询定位”                    新 thread,E/F 在这儿聊
   📌 1701.0501  <慢查询定位> update feed:      feed-fork.md
  ──────────────────────────────────────────────────────────────────────
   tree.json: 1 ──► 1.a          边是 fork 当场写下的,不靠事后推断
```

在 `1.a` 里再 fork 就得到 `1.a.i` —— Slack 装不下的那层嵌套。

### 5 · `tree`:想看多粗看多粗

```
$ /canopy tree                       不带参数 → 所有根,depth 0
  pay-timeout  支付超时     active   4 active / 2 untracked   🔒1

$ /canopy tree pay-timeout           点名一个根 → depth all
  1        支付超时         active     A君
  ├ 1.a    慢查询定位       active     E君
  │ └ 1.a.i  索引方案       active     F君    🔒 worker 正在跑
  └ 1.b    重试风暴         untracked  A君

$ /canopy tree 1.a --depth 1         从哪儿开始、往下几层,是两个独立参数
  ↑ pay-timeout / 1                  面包屑,免得看丢位置
  1.a      慢查询定位       active   E君
  └ 1.a.i  索引方案         active   F君    ▸ 2 untracked

$ /canopy untrack 1.b                不盯了,feed 留着,树上标 ×
$ /canopy track 1.b                  重新盯上
                                     最后一个活跃节点被 untrack 时,cron 自己撤掉
$ /canopy map                        重刷树消息,打印链接
```

### 6 · `return` / `ack return`:结论回灌给上一层

```
🧵 1.a  @canopy return         草稿发成一条新消息,只给 A君 看
        @canopy ack return ──► 发进 🧵 1                  return-post.md
        @canopy untrack    ──► 1.a 的 feed 里发 status-untracked.md
                               trace tree 里标 ×
```

A君没点头之前,什么都不会进父 thread。

`return` 是可选的,而且不留痕。它只是帮你起草那段结论 —— 人自己在父 thread 里把结论
说了,子问题一样算收,而且那才是最自然的做法,summarizer 会像对待任何消息一样把它
记进父节点的 feed。所以没有任何东西等着 return 发生,也没有任何地方显示「这个节点
return 过没有」。只有部分路径维护的状态,是会骗人的状态。

### 7 · feed 记歪了:`recalibrate`

```
$ /canopy recalibrate 1        (或者在 thread 里:@canopy recalibrate)
   分块读完整段历史 → 重建所有 feed 段落
   这是重的逃生口;日常靠的是每 tick 只改最后一段的便宜路径
```

### 8 · `untrack`:收树

```
$ /canopy untrack 1            不再盯它,trace tree 里标 ×
                               整台机器没有活跃节点了,cron 条目也一起撤掉
                               想重开:track 1(cron 自己装回来)
```

## 现状

设计已冻结,`SKILL.md` 是唯一事实来源。`scripts/` 已经实现:Python 3、只用标准库(tick
跑在 cron 里,少一个依赖就是一棵树悄悄不再被盯),`python3 -m pytest` 跑 170 个测试,
不连网、不连 Slack、不调模型。模块分工见
[`scripts/README.md`](./scripts/README.md)。

整棵树的导航面**不是** Slack Canvas —— `slackcli` 只能读 canvas 不能写,而一条只有
渲染它那台机器打得开的链接不如没有。所以是频道里的一条普通消息,原地更新;树深了就
每 4 层切一条新消息,切口那个节点在上一条里变成指针,新消息回指上一条,整棵树点得动。

## License

MIT —— 见 [`LICENSE`](./LICENSE)。
