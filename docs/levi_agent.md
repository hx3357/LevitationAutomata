# 基于Hermes Agent二次开发的Levi Agent ~ 迭代1

该agent有两个特点：

1. 接入微信原生platform adaptor，同时支持私聊和群聊
2. 模拟一个真实的人，能够参与私信/群聊，记忆消息，回应消息，提出话题，模拟日常行程

开发该agent可以帮助我提前预习米哈游ai npc开发的必要知识和经验。也可以迁移项目经验，帮助我构建具备人格的个人助理，同时提供情感陪伴。

---

## 需求分析

### user/**agent** stories

0. **agent拥有自己的灵魂，以模拟真人感为第一目标**，而不是为了成为用户的工具而存在。
1. 用户可以询问有关过去聊天内容的问题，agent会调取之前的记忆，参考记忆给出回答。
2. agent会随着聊天进行，记录、总结并更新群聊中每个用户的性格和个性表，同时估计用户对agent的关注度。
3. 当用户主动@agent或者引用了agent之前发送的内容，agent会认为该用户对自己的反应很感兴趣，根据用户回应改变关注度并考虑给予回复。
4. 管理员可以无条件通过预先定义好的命令来操控agent的行为，如使用 /simplify 来强制压缩agent上下文，使用 /summarize 来强制给出总结群聊内容任务。
5. agent会听取来自群聊中top_k关注度以内的用户提出的，符合自己设定中**感兴趣的内容**的日常行程建议，考虑把它加入日常行程中。
6. agent会定时主动发起新的话题。具体条件如下：
    - agent根据群聊过去的内容，结合互联网上的新闻等**外部**信息来考虑新的话题
    - agent会执行日常行程，分享自己日常行程的时间，内容，结果和感悟。在执行的时候会从空闲状态进入执行状态，专注执行手头的任务，不会主动发消息。回消息的时候也会提及自己在做什么事，回答的内容较空闲状态更简略。

---

## 架构设计

### 原则

1. 为了应对hermes频繁更新的现状，对于hermes的代码库保持最小侵入原则，尽量以插件，hook的形式实现。在hermes api不变的前提下，更新后魔改部分可以0成本中继。
    [hermes扩展文档](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing)

### wx adaptor

通过plugin platform adaptor实现。
[实现文档](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)

### 上下文工程

一个session对应一个群聊/私信。session上下文中包含最近的聊天记录和从长期记忆调取的信息。

### 真人感模拟

1. 关注度系统
2. 日常行程系统

---

## 技术实现

## 开发计划

- [x] 实现wx platform adapter
- [ ] 实现数据模块：微信聊天记录数据库解析，同步，实时记录功能。
- [ ] 实现记忆模块：接入honcho（数据模块和记忆模块有必要分开吗？）
- [ ] 实现群聊bot：设计agent内部思考流程，回复触发阀门，从而决定是否触发agent loop
