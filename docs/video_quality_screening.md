# 视频质量粗筛系统架构

## 概述

粗筛系统位于 `scripts/ad_assets.py:415`，核心作用是**用规则圈出可能有问题的时间段**，而不是真正"看懂视频"。

## 四层检测机制

### 1. 运动档位选择（Motion Profile Selection）

**位置：** `ad_assets.py:171`

根据镜头运动类型选择阈值档位：

- **static** - 静镜头，阈值最紧
- **medium_motion** - 普通运动（pan, tilt, dolly, zoom 等）
- **heavy_motion** - 强运动（orbital, crane, rapid, tracking）

**阈值参数：** `ad_assets.py:33`

每个档位控制：
- MSE 阈值（threshold_base, threshold_multiplier）
- 连续异常帧数（consecutive_bad）
- 缓冲帧数（grace_window_frames）
- 闪烁判定（min_amplitude, reversal_count）
- 突变判定（spike_factor, spike_abs_min）

---

### 2. MSE 系规则（Frame Difference Detection）

**位置：** `ad_assets.py:415` 前半段

**原理：** 计算相邻帧的像素差（MSE），用前 1/4 帧的中位数作为基线，乘以档位 multiplier 得到阈值。

#### 2.1 反向尾段扫描（Reverse Tail Scan）

**位置：** `ad_assets.py:460`

**作用：** 从后往前找"最后一段稳定区域"，判断尾部是否崩溃。

**触发条件：**
- 尾部连续多帧 MSE 超过阈值
- 超过 `required_stable` 帧数

**输出：** `trim_to` 时间点

---

#### 2.2 正向连续异常检测（Forward Consecutive Anomaly）

**位置：** `ad_assets.py:494`

**作用：** 检测中间段连续高 MSE 区域。

**触发条件：**
- 连续 `consecutive_bad` 帧超过阈值
- 经过 `grace_window_frames` 缓冲后仍异常

**输出：** `bad_segments` 或 `risk_segments`

---

#### 2.3 Motion Ramp 豁免（Motion Ramp Exemption）

**位置：** `ad_assets.py:556`

**作用：** 如果画面变化是连续上升、单调增强（推近/旋转/加速），不判定为异常。

**判断标准：**
- MSE 连续上升
- 斜率稳定
- 符合镜头运动预期

**目的：** 减少"强镜头运动被误判"的情况。

---

#### 2.4 闪烁检测（Flicker Detection）

**位置：** `ad_assets.py:585`

**作用：** 检测高低高低反复跳动的帧间差。

**触发条件：**
- 连续 `reversal_count` 次反转
- 振幅超过 `min_amplitude_base * min_amplitude_multiplier`

**典型问题：**
- 闪烁
- 抖动
- 奇偶帧不稳定

**输出：** `flicker_segments`

---

#### 2.5 局部突变检测（Spike Detection）

**位置：** `ad_assets.py:621`

**作用：** 检测单帧突然比周围局部均值高很多的情况。

**触发条件：**
- 某帧 MSE > 局部均值 * `spike_factor`
- 且 MSE > `spike_abs_min`

**典型问题：**
- 瞬时变脸
- 单帧结构跳变
- 闪现异常

**输出：** `spike_segments`

---

### 3. 语义启发式规则（Semantic Heuristics）

**位置：** `ad_assets.py:1020`

使用 OpenCV 传统方法做低成本抽样检测，**不是大模型理解，只是启发式**。

#### 3.1 身份幻觉（Identity Hallucination）

**位置：** `ad_assets.py:1098`

**逻辑：**
- 检测到的人脸数 > 预期人数
- 比较脸 crop 是否非常相似

**目的：** 抓"同一个人凭空复制出两个"。

---

#### 3.2 脸部身份漂移（Face Identity Drift）

**位置：** `ad_assets.py:1147`

**逻辑：**
- 拿早期 anchor face 和后续主脸做相似度比对
- 连续低分记为身份漂移

**典型问题：** 角色脸逐渐变成另一个人。

---

#### 3.3 脸部朝向不连续（Face Orientation Discontinuity）

**位置：** `ad_assets.py:1178`

**逻辑：**
- 根据 storyboard 的 `motion_control.subject_facing` 判断预期朝向
- 粗判人脸应该朝前/左/右
- 连续抽样方向不对且反复 flip

**典型问题：** 脸部朝向突然跳变。

---

#### 3.4 头身不一致（Head-Body Inconsistency）

**位置：** `ad_assets.py:1217`

**逻辑：**
- 朝向突然跳
- 同时 identity score 也在掉

**典型问题：** 头和身体不匹配。

---

### 4. DNN 人脸检测（Face DNN Warning）

**位置：** `ad_assets.py:793`

**作用：**
- 前段时间稳定检测到脸
- 后段长时间检测不到脸

**当前状态：** 只记录 `face_dnn_warning_time`，不强制裁剪。

**原因：** "后面没脸"不等于视频坏了（转身、远景、背身都可能导致检测不到脸）。

---

## 输出结果

**位置：** `ad_assets.py:452` 和 `ad_assets.py:767`

粗筛系统输出：
- `bad_segments` - 明确的坏片段
- `risk_segments` - 可疑片段
- `cut_segments` - 建议裁剪的片段
- `trim_to` - 建议裁尾的时间点

---

## 两种工作模式

**位置：** `ad_assets.py:886`

### metrics_only 模式
- 规则层直接决定：保留、裁尾、局部裁、重生成

### hybrid_judge 模式
- 规则层只导出 `risk_segments`
- 不直接执行
- 交给视觉模型（母基模）裁定

**Risk Bundle 导出：** `ad_assets.py:1415`

---

## 系统优势与局限

### 优势
- ✅ 快速圈出风险区域
- ✅ 成本低（纯规则 + OpenCV）
- ✅ 可配置（不同运动档位）

### 局限
- ❌ 会误伤强镜头运动（shot_001/004/005）
- ❌ 会漏掉语义化问题（shot_006 attempt_2："身体动作对，但脸已经换了"）
- ❌ 不真正理解视频内容

---

## 下一步：视觉裁定层

**角色分工：**

| 层级 | 负责 | 输出 |
|------|------|------|
| **规则粗筛层**<br>(`_scan_video_quality()`) | 哪里可能有问题 | 风险时间段、风险原因 |
| **视觉裁定层**<br>(母基模 LLM) | 这些风险是不是真的 | keep / cut_segment / trim_tail / regenerate |

**工作流程：**
1. 规则粗筛圈出 risk_segments
2. 提取关键帧
3. 母基模看帧判断
4. 输出最终动作
