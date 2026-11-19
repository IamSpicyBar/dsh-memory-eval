# Memory RSI · dsh-memory-eval 0.3.0

0.3.0 将实验控制台接入 Studio 的 Memory 子平台：使用 `start --studio-origin https://hmbotstudio.cn` 明确允许该站嵌入后，在子平台点击连接。本机服务仍只监听回环地址，API 仍要求同源与令牌；父页面只接收版本就绪消息，凭据和轨迹不经过网站服务器。独立控制台也可照常使用。

默认流程仍为 L1 + L2 内容验证。L3 是可选的历史 Agent 回放扩展：默认 setup 不获取 L3 依赖；未配置、未运行、待复核与失败分别记录。L3 不可用或失败不会改变主流程状态，也不阻止候选归档。只有新建实验时明确选择 agent-replay 验证目标，L3 才进入该目标的已验证发布门禁。所有上传都保留实际 validationTarget / validationScope / l3State。

从真实离线轨迹或数据飞轮选择会话，使用独立 MigLoop 内核分析、制卡与归并，再做 L1/L2 评测，并将明确预览的 Memory 版本上传到私有 GitHub 仓库。本插件同时提供 DSH 工具和本地实验控制台。

组件版本由 runtime/dependencies.json 固定。配套组件为 [Studio dsh-fixchain](https://github.com/IamSpicyBar/dsh-fixchain) 与 [Studio dsh-migloop](https://github.com/IamSpicyBar/dsh-migloop)。内核直接使用公开 hxy450/migloop@c72c0db 的 Memory skills 0.10.10 与 DSH 原生解析器，**无需 a2h-memory 仓库或其分支**。不复制或重写原有制卡、归并算法。

## 从发布包开始

需要 Node.js 24.9+、Python 3.10+、Git、npm 和已登录的 GitHub CLI。macOS/Linux；Windows 请使用 WSL2。

    gh auth status
    gh release download v0.3.0 --repo IamSpicyBar/dsh-memory-eval --pattern 'memory-rsi-0.3.0.tgz'
    tar -xzf memory-rsi-0.3.0.tgz
    cd package
    node bin/studio.mjs setup
    node bin/studio.mjs start --studio-origin https://hmbotstudio.cn

打开终端显示的本机地址，默认 http://127.0.0.1:8878。启动器只监听回环地址，不修改已有 DSH。可用 --port 8881 更换端口；HMBOT_MEMORY_HOME 指定独立状态目录。已有相同版本 DSH 可用 setup --dsh-cli /absolute/.../dsh/lib/bin.js 只读复用。

setup 自动下载固定源版本，建立独立 Python / PyYAML 6.0.3 环境，构建可移动 skills 和 MigLoop zipapp，并安装 DSH 0.2.2-rc.1。首次准备完整 DSH 占用约 1 GiB，建议至少预留 3 GiB。L2 复用 fuxi-ailabs/memory-eval@d226198 的两个固定 SHA256 模块；该仓库目前为私有，因此当前 GitHub 账号需要读取权限。权限不足会明确报错，不替换评分实现或复制私有源码到本仓。

## 一轮操作

1. **连接数据飞轮**：填写 Agent Performance API 根地址，例如 https://your-server/api/v1/agent-perf/，以及该平台的 Bearer token。需要 dashboard:read 和会话导出权限。连接令牌只存在本地进程中，不写入配置、日志、GitHub，也不发给对象存储。没有连接时，选择已有 a2h-export/4 导出包或原生会话目录。
2. **选择项目和会话并冻结**：按项目搜索、分页选择业务 Session，调用现有 /projects、/sessions、POST /export-stages。兼容直接 gzip 响应与短期 presigned URL。逐 run/session/epoch 核对大小、SHA256 和附件；缺片或来源身份不符会停止。URL 不作为固定来源身份。超过默认 4 GiB 的导出需另行调整预算。
3. **查看轨迹并制卡**：控制台调用 fork 的 dsh-migloop 生成只读报告，调用 fork 的 dsh-fixchain 准备材料，使用真实 headless DSH 主控和独立子代理调查。作者模型、请求预算在来源冻结时记录。默认本轮只调查一个问题，不能据此宣称覆盖整次迁移。历史工程 refs 可选提供，原始材料不改写。
4. **生成或导入 L2 题集**：自动出题是另一个独立 DSH 会话，最多 20 次请求、最多一道派生题；也可填已有任务、历史失败和正确参考路径。每条断言必须在失败样本上失败、在参考样本上通过，否则不开始模型评估。
5. **冻结并运行 L1 / L2**：选择与作者不同的真实判官模型及端点。DeepSeek 可配置作者 deepseek-flash、判官 deepseek-v4-pro，名称以[官方模型文档](https://api-docs.deepseek.com/quick_start/pricing/)为准；其他 Anthropic Messages 兼容端点也可。两组凭据均只存在进程内。每个评测臂一次请求，不自动重试或复用回答；本实验上限 L1 四条经验 × 三次，L2 六次请求。
6. **人工复核及上传**：查看冻结 L1 证据与模板，逐条填写 R1–R8 的结论、经验引用、证据引用和理由。未校准或有争议的自动判定不能自动发布。上传前查看文件摘要；可存为“候选”，或在 L1 通过/明确人工复核且 L2 严格改善后标为“已验证”。用本机 gh 登录上传，只接受私有 Memory 仓库。

候选上传不代表验证成功。上传阅读包、版本 store、来源卡与评测摘要，不上传完整会话、模型原始回答或凭据。GitHub 采用新 commit 与非强制 ref 更新；发生并发冲突时停止，保留本地证据。CANDIDATE.json 和 LATEST.json 分别指向候选与已验证版本。

## L1 与 L2 的含义

- **M0**：原生 mechanical lint；原生 Memory snapshot、卡片内容哈希和 claim/revision 绑定。lint 命中需先修订，不能靠删除内容“过检”。
- **R1–R7**：证据支持、适用边界、可执行性、不写经过、短召回呈现、库内一致与修订理由。每条经验三次独立判定，必须逐项返回结构化结论与精确原文引用。缺项、截断、伪造引用、版本摘要不符都是 invalid。判官与作者必须不同。校准记录需匹配本判官和规则摘要，阻断规则与人工标注一致率至少 0.85；无记录时结果只供复核，不能自动晋升。
- **R8**：通过华为官方开发者知识 MCP 检索冻结文档，逐条平台断言输出 supported / contradicted / not-covered / version-mismatch / runtime-observed / n/a。缺文档不等于事实正确，版本差异与真实运行时观测单列，不自动改 Memory。
- **L2**：模型只获得冻结任务、输入和指定 lessons；看不到来源轨迹、检查或参考答案。精确目标文件、end_turn、suite/prompt/reply 哈希全部绑定，至少一项改善、没有回退且配对全有效才接受。显式指定经验是内容传递测试，不是自主召回。

L1 自动判官的校准集构建和跨模型校准实验尚未内置；当前提供外部校准记录接口与完整人工复核路径。一次开发集改善不等于跨应用泛化。L3 agent 回放、编译与设备测试需另外配置环境，不由 L2 推断。

## 可选 L3（实验性）

按需运行 `node bin/studio.mjs setup --with-l3`，额外获取固定私有 agent-replay@39d6237；默认安装不需要该仓库权限。真正回放还需用户自己的 Claude Code、对应模型端点和独立 L3 凭据。先在默认流程完成来源、卡片、Memory 和冻结 L2，再展开可选 L3。纯转录不能补造历史工程快照。

prepare 只读取录制、生成原生 first-write / dispatch 回放工程，并核验历史 agent 的 end 产物确实匹配校准失败任务，不调用模型。派生 L2 小题若不是原 Agent 的实际目标文件，会明确拒绝 L3 准备，默认流程仍可继续。run 显式执行 B0 原样、B1 阅读包入口、B2 系统追加三臂，各三次；B2 保留原 agent 定义。逐次限定轮数及超时、不自动重试、不使用 --no-guard 或 --skip-permissions；权限不足、中断或截断保留为不完整。原生写入 guard 是回放工具保护，不能替代按需配置的操作系统隔离环境。

score 对真实产物复用校准文本检查，并绑定 suite / 原始 trace / 完成元数据 / 产物哈希。只要原点 B0 已能避开偏差，就记原点不稳定；模型不符或保真度未复核不接受经验效应。至少一个经验臂 2/3 避开偏差、无新增检查回退，并完成“是否读到／采用／误用”的逐次过程复核后才可能 accepted。首次重评生成 process-review-template.json，模板不预填为通过；人工填完后保存为 process-review.json，再次 score。当前没有自动的已校准过程判官。

L3 是评估验证阶段可调用的一种执行方式；它与 Skill 实跑评测都可使用独立执行环境和领域检查器。当前版本提供原生 Claude Code 回放适配及离线门禁测试，未宣称完成新的真实 L3 模型试验，也未覆盖 PPT 视觉、编译、设备或留出应用泛化。默认内容验证的已验证发布仍要求 L1 + L2，摘要明确其范围。

0.2.2 的 L1 判官协议为 hmbot.memory.l1-judge/2：R1–R7 只出现在 rules，R8 只出现在 facts。判官引用冻结的 L/E/D 原文 ID，评分器核对 ID、原文、文档归属及 suite 哈希；不对模型伪造的引文做模糊匹配。官方完整检索结果仍冻结保存，发送判官的是按经验中 API 名称选择的逐字文档片段。旧协议校准记录不能用于新协议。

真实 2048 试跑中，0.2.0 主控在派工前耗尽 30 次预算，之后由 Codex 宿主按相同公开 skills 接管独立制卡与归并；这是有宿主介入的流程，不是无人干预成功。原始失败及预算保留，没有重置调用额度。单阶段 30 次只是首轮预算，不能保证任意大小材料都能完成自动制卡。0.2.2 修正首次 L1 判官提示的结构歧义，原始无效回答不会因此转正。

## 单独安装为 DSH 插件

    dsh plugin --profile YOUR_PROFILE add /absolute/path/to/package

memory_eval 参数分别为：

    {"action":"prepare","config":"/absolute/l2-config.json","output":"/new/l2-suite"}
    {"action":"run","suite":"/absolute/l2-suite","output":"/new/l2-replies"}
    {"action":"score","suite":"/absolute/l2-suite","output":"/existing/l2-replies"}
    {"action":"l1-prepare","config":"/absolute/l1-config.json","output":"/new/l1-suite"}
    {"action":"l1-run","suite":"/absolute/l1-suite","output":"/new/l1-replies"}
    {"action":"l1-score","suite":"/absolute/l1-suite","output":"/existing/l1-replies"}
    {"action":"l3-prepare","config":"/absolute/l3-config.json","output":"/new/l3-suite"}
    {"action":"l3-run","suite":"/absolute/l3-suite","output":"/new/l3-replays"}
    {"action":"l3-score","suite":"/absolute/l3-suite","output":"/existing/l3-replays"}

也可直接运行 python evaluate.py prepare|run|score 或 python l1.py prepare|run|score。模型凭据使用 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY 环境变量；DSH 制卡使用 DEEPSEEK_API_KEY。prepare 与 score 不调用模型；带 factQueries 的 L1 prepare 会读取官方公开文档。

## 验证

    node --test tests/*.test.mjs
    python3 -m unittest discover -s tests -p 'test_*.py'

测试包括跨源/Host/令牌边界、presigned 下载凭据隔离、超预算流、Memory 发布门禁、逐项 L1 与原文引用绑定。真实飞轮连接取决于用户填写的地址和权限；契约测试不能替代生产飞轮联调。

0.2.2 还禁止评测进程向固定后端 checkout 写 Python 字节码，避免真实评测后再次 setup / 升级被自身生成的缓存阻断。
