"""
双语受控词表（catalog）+ 平台/assay 归一规则 + 停用词。

设计要点（经对抗评审收敛）：
- hard_filter 的可靠性 = 约束抽取的召回率。词表尽量覆盖语料真实词汇 + 常见别名，
  并**收录库中不存在的已知实体**（如 elephant/大象），好让解析器"看见"约束→正确返回无结果。
- platform_family（Visium/Xenium/Chromium/Atera）与 assay（ATAC/Multiome/Flex/CNV/GEX…）
  是两个不同维度，分开建模，不再混用凌乱的 chemistry 串。
- FILLER 用于 fail-closed 残差门：命中所有词表项后，若查询仍剩"有意义的未识别词"，
  则宁可 abstain（无结果+原因），也不静默丢弃约束。
"""
from __future__ import annotations

# ---------- 规范化规则：从 record 派生 platform_family / assay ----------
# 作用于 raw['platform']（干净）
PLATFORM_FAMILY_RULES: list[tuple[str, str]] = [
    ("visium", "visium"),
    ("xenium", "xenium"),
    ("atera", "atera"),
    ("chromium", "chromium"),
]
# 作用于 chemistry（凌乱），best-effort 归类到 assay
ASSAY_RULES: list[tuple[str, str]] = [
    ("multiome", "multiome"),
    ("atac", "atac"),
    ("flex", "flex"),
    ("cnv", "cnv"),
    ("in situ", "in_situ"),
    ("spatial", "spatial_gex"),
    ("immune profiling", "immune"),
    ("de novo", "de_novo"),
    ("genome & exome", "genome_exome"),
    ("5'", "gex_5p"),
    ("3'", "gex_3p"),
    ("gene expression", "gex"),
]


def derive_platform_family(platform_raw: str) -> str:
    text = (platform_raw or "").lower()
    for key, canonical in PLATFORM_FAMILY_RULES:
        if key in text:
            return canonical
    return ""


def derive_assay(chemistry: str) -> str:
    text = (chemistry or "").lower()
    for key, canonical in ASSAY_RULES:
        if key in text:
            return canonical
    return ""


# ---------- modality（数据模态：解离式单细胞 / 空间·原位 / 未知）----------
# 背景（验证反馈 + 3 视角验证收敛）：`单细胞` 原硬映射成 platform=chromium，多源检索会静默排除
# 非 10x 单细胞技术（Smart-seq2/Drop-seq/sci-RNA-seq…）。改成独立 modality 维：单细胞=**解离式悬液**模态，
# 不等于某一平台。**base 767 全有 platform_family** → 只走规则 1/2（chromium→single-cell；visium/xenium/atera→
# spatial），派生纯确定、冻结门逐位不变；chemistry 扫描仅对外部（platform_family 为空）记录生效。
# 顺序载荷（实测外部 chemistry 分布）：spatial → 单细胞正信号 → bulk 排除 → 无信号 fail-CLOSED（=""，与
# has_raw_data==None 的 fail-closed 不变量一致）。关键：`rna-seq of coding rna from single cells`(388) 必须先被
# 单细胞正信号命中，才不会被 `rna-seq of coding rna`(760, bulk) 误排除——故单细胞正信号在 bulk 之前判。
_MODALITY_SPATIAL_MARKERS = (
    "visium", "xenium", "atera", "slide-seq", "slide-seqv2", "stereo-seq", "merfish",
    "seqfish", "starmap", "cosmx", "geomx", "dbit-seq", "hdst", "osmfish", "curio",
    "spatial", "in situ", "in-situ",
)
_MODALITY_SINGLECELL_MARKERS = (
    "single cell", "single-cell", "single cells", "single nucleus", "single-nucleus",
    "snrna", "sn-rna", "snmc", "scrna", "sc-rna", "scatac", "sc-atac",
    "smart-seq", "smartseq", "smart-like", "drop-seq", "dropseq",
    "sci-rna-seq", "sci-rna", "seq-well", "seqwell", "cel-seq", "indrop", "in-drop",
    "mars-seq", "cite-seq", "citeseq", "fluidigm c1", "bd rhapsody",
    "gexscope", "strt-seq", "microwell", "split-seq", "10x", "chromium",
)
_MODALITY_BULK_MARKERS = (
    "rna-seq of coding rna", "rna-seq of non coding rna", "rna-seq of non-coding rna",
    "rna-seq of total rna", "non coding rna", "non-coding rna",
    "transcription profiling by array", "microarray", "chip-seq", "chip seq",
    "dna-seq", "methylation", "bisulfite", "microrna", "rip-seq", "ribo-seq",
    "proteomic", "wgs", "wes", "high-throughput sequencing",
)


def derive_modality(platform_family: str, chem_text: str) -> str:
    """数据模态：'single-cell' / 'spatial' / ''（未知，fail-closed）。单一真源，normalizer 存字段、
    retriever 读存字段、评测裁判从 raw 独立重算三方共用。base 全走 platform_family（确定），外部才扫 chemistry。"""
    pf = (platform_family or "").strip().lower()
    if pf in ("visium", "xenium", "atera"):
        return "spatial"
    if pf == "chromium":
        return "single-cell"
    # 外部（platform_family 为空）：按序扫 chemistry/study_type 文本。
    t = (chem_text or "").strip().lower()
    if not t:
        return ""
    if any(m in t for m in _MODALITY_SPATIAL_MARKERS):
        return "spatial"
    if any(m in t for m in _MODALITY_SINGLECELL_MARKERS):
        return "single-cell"
    if any(m in t for m in _MODALITY_BULK_MARKERS):
        return ""   # bulk/array/ChIP/methylation… → 非单细胞，排除
    return ""       # 无信号 → fail-closed（不臆断为单细胞）


# ---------- 受控词表：alias(中/英) -> 该维度字段应包含的规范 target ----------
# 匹配语义：查询命中任一 alias => 该维度约束 = targets；hard_filter 要求对应记录字段包含任一 target。
# alias 覆盖扩展（tissue+species）：并入 Uberon(组织)/NCBI Taxonomy(物种) 常见同义词 + 中文口语/临床变体（如
#   homo sapiens/mus musculus/表皮/乳房/骨骼肌…）。只扩 aliases 提召回，**不动 targets(规范键)**；匹配是子串+长优先消费，
#   故新增同义词只会把原本会 fail-closed 弃权的说法正确解析，绝不改变 hard_filter 的 0% 违规保证。
# alias 覆盖扩展（外部平台库对齐）：基础库(10x)为 base、外部平台库(CELLxGENE/HCA/EBI SCEA/ArrayExpress/ENCODE，约4900条)
#   含大量 base 没有的 species/tissue/disease/技术（拟南芥/线虫/酵母/胸腺/小脑/视网膜/新冠/阿尔茨海默/帕金森/糖尿病/
#   Smart-seq2/Drop-seq/Slide-seq/MERFISH…）。这些说法此前一律 fail-closed 弃权 → 漏掉数百条真实外部记录。**新增受控项**
#   （非仅扩别名）：每条仍是「alias→子串 target」结构，0% 违规由 hard_filter 结构性保证不变；新 target 只让对应约束能被识别+硬过滤。
#   技术类（Smart-seq2 等）归入 assay 维度（匹配 assay+chemistry 子串），不走 platform 的精确 family 匹配，避免改核心归一。
#   纪律：每次扩充后跑 scripts/evaluate_recommendation.py（54 题冻结门，base-only），确认 Constraint_Violation 仍=0%、Top5/NoResult 不退化，再合。
CATALOG: dict[str, list[dict[str, object]]] = {
    "species": [
        {"aliases": ["人类", "人", "人体", "human", "homo sapiens", "sapiens"], "targets": ["human"], "display": "Human"},
        {"aliases": ["小鼠", "鼠", "老鼠", "mouse", "mice", "murine", "mus musculus", "musculus"], "targets": ["mouse"], "display": "Mouse"},
        {"aliases": ["斑马鱼", "zebrafish", "danio rerio", "danio"], "targets": ["zebrafish"], "display": "Zebrafish"},
        {"aliases": ["大鼠", "rat", "norvegicus", "wistar", "rattus"], "targets": ["rat", "norvegicus"], "display": "Rat"},
        {"aliases": ["恒河猴", "猕猴", "猴", "macaque", "rhesus", "macaca", "mulatta"], "targets": ["macaque", "rhesus"], "display": "Macaque"},
        {"aliases": ["狗", "犬", "dog", "canine", "canis", "canis familiaris"], "targets": ["dog"], "display": "Dog"},
        {"aliases": ["果蝇", "drosophila", "fruit fly", "melanogaster"], "targets": ["drosophila"], "display": "Drosophila"},
        # 模式植物/微生物/其它模式动物：基础库(10x)无、外部平台库(CELLxGENE/HCA/EBI/ArrayExpress/ENCODE)有，
        # 收录以便正确解析并检出外部记录（此前这些说法会 fail-closed 弃权 → 漏掉数百条真实数据）。
        {"aliases": ["拟南芥", "arabidopsis", "thaliana"], "targets": ["arabidopsis", "thaliana"], "display": "Arabidopsis"},
        # 玉米（R6，dev 集 dv28/dv29/dv33）：base 有 2 条 species="Maize" 记录，
        # 此前「玉米 / maize」均未收录 → 整句 unresolved_term 弃权。"zea" 3 字符但有 ASCII 词边界保护。
        {"aliases": ["玉米", "maize", "zea mays", "zea"], "targets": ["maize", "zea mays"], "display": "Maize"},
        {"aliases": ["水稻", "oryza sativa", "oryza"], "targets": ["oryza"], "display": "Rice"},
        {"aliases": ["线虫", "秀丽隐杆线虫", "caenorhabditis", "c. elegans", "c.elegans", "elegans", "nematode"], "targets": ["elegans", "caenorhabditis"], "display": "C. elegans"},
        {"aliases": ["酵母", "酿酒酵母", "yeast", "saccharomyces", "cerevisiae"], "targets": ["saccharomyces"], "display": "Yeast"},
        {"aliases": ["狨", "狨猴", "marmoset", "callithrix"], "targets": ["marmoset", "callithrix"], "display": "Marmoset"},
        {"aliases": ["鼠狐猴", "mouse lemur", "microcebus"], "targets": ["microcebus"], "display": "Mouse lemur"},
        {"aliases": ["黑猩猩", "chimpanzee", "troglodytes"], "targets": ["chimpanzee", "troglodytes"], "display": "Chimpanzee"},
        # 非人灵长类（NHP）伞术语：映射到库中已有各灵长类物种子串之并集。词首「非人/non-human」是词素、非排除操作符
        # （此前误报 unsupported_negation）。base 仅 macaque(5) 命中，marmoset/lemur/chimpanzee 在外部平台库；targets 均为
        # species 字段子串、都不含 human → 「非人」语义精确。只收「非人/non-human/NHP」形，不收裸「灵长类/primate」(含人)。
        # 保留 3 字缩写 "nhp"（NHP 是本域高频写法）：其唯一子串碰撞是人类基因 NHP2，属**域外**词（本工具按物种/组织/
        # 疾病检索数据集、不按基因符号查）→ 与既有 gbm/aml/crc 等短别名同量级、可接受；不同于已剔除的 nhl↔NHLBI（域内机构名）。
        {"aliases": ["非人灵长类", "非人类灵长类", "非人灵长", "非人猿类",
                     "non-human primate", "non-human primates", "non human primate",
                     "nonhuman primate", "nhp"],
         "targets": ["macaque", "rhesus", "mulatta", "marmoset", "callithrix",
                     "microcebus", "chimpanzee", "troglodytes", "cynomolgus", "fascicularis", "primate"],
         "display": "Non-human Primate"},
        {"aliases": ["爪蟾", "非洲爪蟾", "xenopus"], "targets": ["xenopus"], "display": "Xenopus"},
        # 库中不存在但已知的物种：收录以便"看见"约束→正确无结果（fail-closed 回归）
        {"aliases": ["大象", "象", "elephant"], "targets": ["elephant"], "display": "Elephant", "absent": True},
        {"aliases": ["猪", "pig", "porcine", "swine"], "targets": ["pig"], "display": "Pig", "absent": True},
        {"aliases": ["牛", "cattle", "bovine"], "targets": ["cattle", "bovine"], "display": "Cattle", "absent": True},
        {"aliases": ["鸡", "chicken", "avian"], "targets": ["chicken"], "display": "Chicken", "absent": True},
        {"aliases": ["兔", "rabbit"], "targets": ["rabbit"], "display": "Rabbit", "absent": True},
    ],
    "tissue": [
        {"aliases": ["乳腺", "乳房", "breast", "mammary", "mammary gland"], "targets": ["breast"], "display": "Breast"},
        {"aliases": ["肺", "肺部", "lung", "pulmonary"], "targets": ["lung"], "display": "Lung"},
        {"aliases": ["脑", "大脑", "脑组织", "brain", "cortex", "cerebral", "cerebrum"], "targets": ["brain"], "display": "Brain"},
        {"aliases": ["肝", "肝脏", "liver", "hepatic"], "targets": ["liver"], "display": "Liver"},
        {"aliases": ["肾", "肾脏", "kidney", "renal"], "targets": ["kidney"], "display": "Kidney"},
        {"aliases": ["心", "心脏", "heart", "cardiac"], "targets": ["heart"], "display": "Heart"},
        {"aliases": ["血", "血液", "外周血", "blood", "pbmc", "peripheral blood"], "targets": ["blood", "pbmc"], "display": "Blood"},
        {"aliases": ["皮肤", "表皮", "skin", "cutaneous", "dermal", "epidermal", "epidermis", "dermis"], "targets": ["skin"], "display": "Skin"},
        {"aliases": ["骨髓", "bone marrow", "marrow"], "targets": ["bone marrow"], "display": "Bone Marrow"},
        {"aliases": ["卵巢", "ovary", "ovarian"], "targets": ["ovary", "ovarian"], "display": "Ovary"},
        {"aliases": ["淋巴结", "lymph node"], "targets": ["lymph node"], "display": "Lymph Node"},
        {"aliases": ["脾", "脾脏", "spleen", "splenic"], "targets": ["spleen"], "display": "Spleen"},
        {"aliases": ["胰", "胰腺", "pancreas", "pancreatic"], "targets": ["pancreas"], "display": "Pancreas"},
        {"aliases": ["胃", "stomach", "gastric"], "targets": ["stomach", "gastric"], "display": "Stomach"},
        {"aliases": ["前列腺", "prostate", "prostatic"], "targets": ["prostate"], "display": "Prostate"},
        {"aliases": ["结肠", "肠", "colon", "colorectal", "intestine"], "targets": ["colon", "intestin"], "display": "Colon"},
        {"aliases": ["宫颈", "子宫颈", "cervix", "cervical"], "targets": ["cervix", "cervical"], "display": "Cervix"},
        {"aliases": ["胚胎", "胚", "embryo", "embryonic"], "targets": ["embryo"], "display": "Embryo"},
        {"aliases": ["扁桃体", "tonsil", "tonsillar"], "targets": ["tonsil"], "display": "Tonsil"},
        {"aliases": ["肌肉", "骨骼肌", "muscle", "skeletal muscle"], "targets": ["muscle"], "display": "Muscle"},
        # 外部平台库常见组织（基础库少见/无 → 补齐使外部记录可被解析检出；均为子串硬过滤，长优先消费防串味）
        {"aliases": ["胸腺", "thymus", "thymic"], "targets": ["thymus"], "display": "Thymus"},
        {"aliases": ["小脑组织", "小脑", "cerebellum", "cerebellar"], "targets": ["cerebell"], "display": "Cerebellum"},   # "小脑组织"须先于 brain 的 "脑组织" 别名被消费
        {"aliases": ["海马", "海马体", "hippocampus", "hippocampal"], "targets": ["hippocamp"], "display": "Hippocampus"},
        {"aliases": ["视网膜", "retina", "retinal"], "targets": ["retina"], "display": "Retina"},
        {"aliases": ["眼睛", "眼部"], "targets": ["eye"], "display": "Eye"},   # 去掉裸「眼」(撞青光眼/转眼/龙眼)与「eye」(子串撞 Meyer/surveyed)：只留双字词，target 仍为 eye 故 eye 记录照样可达
        {"aliases": ["脊髓", "spinal cord", "spinal"], "targets": ["spinal"], "display": "Spinal Cord"},
        {"aliases": ["睾丸", "testis", "testicular"], "targets": ["testis", "testicular"], "display": "Testis"},
        {"aliases": ["回肠", "ileum", "ileal"], "targets": ["ileum"], "display": "Ileum"},
        {"aliases": ["十二指肠", "duodenum", "duodenal"], "targets": ["duodenum"], "display": "Duodenum"},
        {"aliases": ["直肠", "rectum", "rectal"], "targets": ["rectum", "rectal"], "display": "Rectum"},
        {"aliases": ["气管", "trachea", "tracheal"], "targets": ["trachea"], "display": "Trachea"},
        {"aliases": ["支气管", "bronchus", "bronchi", "bronchial"], "targets": ["bronch"], "display": "Bronchus"},
        {"aliases": ["脂肪", "脂肪组织", "adipose", "adipose tissue"], "targets": ["adipose"], "display": "Adipose"},
        {"aliases": ["主动脉", "aorta", "aortic"], "targets": ["aort"], "display": "Aorta"},
        {"aliases": ["子宫", "uterus", "uterine"], "targets": ["uterus", "uterine"], "display": "Uterus"},
        {"aliases": ["甲状腺", "thyroid"], "targets": ["thyroid"], "display": "Thyroid"},
        {"aliases": ["食管", "食道", "esophagus", "esophageal", "oesophagus"], "targets": ["esophag", "oesophag"], "display": "Esophagus"},
        {"aliases": ["胎盘", "placenta", "placental"], "targets": ["placenta"], "display": "Placenta"},
        # ── 语料实证覆盖扩充（组织）──────────────────────────────
        # 挑选依据不是拍脑袋，是**查询侧覆盖率实测**：拿 106 个常见中文检索词逐个跑 parse_query，
        # 挑出「语料里确实有数据、却因为词表没这个词而整句 unresolved_term 弃权」的那些
        # （皮层 332 条 / 口腔 97 / 纹状体 72 / 膀胱 35 / 肾上腺 28 / 下丘脑 25 …共 66 个）。
        # 每个 target 子串都回语料核对过它到底命中哪些取值，专门防「过宽」：
        #   · 裸 "oral" 会命中 middle temp**oral** gyrus（39 条颞叶数据混进口腔）→ 改用 oral cavity/buccal/mouth；
        #   · 裸 "caudate" 会命中 caudate lobe of liver（29 条肝尾状叶混进纹状体）→ 改用 caudate nucleus/-putamen；
        #   · 裸 "bone" 里 189/225 是 bone marrow（已有独立概念）→ **不收**「骨」，宁可继续弃权。
        # 同理**不收**「白质」：它是「蛋**白质**」的真子串，会把蛋白质组学查询劫持成组织=白质
        # （正是要消灭的那类事故），只收无歧义的「脑白质」形不值当，留待后续保护机制落地后再议。
        {"aliases": ["膀胱", "bladder", "urinary bladder"], "targets": ["bladder"], "display": "Bladder"},
        {"aliases": ["耳蜗", "内耳", "cochlea", "cochlear"], "targets": ["cochlea"], "display": "Cochlea"},
        {"aliases": ["肾上腺", "adrenal", "adrenal gland"], "targets": ["adrenal"], "display": "Adrenal Gland"},
        {"aliases": ["下丘脑", "hypothalamus", "hypothalamic"], "targets": ["hypothalam"], "display": "Hypothalamus"},
        {"aliases": ["杏仁核", "amygdala"], "targets": ["amygdala"], "display": "Amygdala"},
        {"aliases": ["纹状体", "striatum", "caudate nucleus", "caudate-putamen", "putamen"],
         "targets": ["striatum", "caudate nucleus", "caudate-putamen", "putamen"], "display": "Striatum"},
        {"aliases": ["垂体", "脑垂体", "pituitary"], "targets": ["pituitary"], "display": "Pituitary"},
        {"aliases": ["嗅球", "嗅觉", "olfactory"], "targets": ["olfactory"], "display": "Olfactory"},
        {"aliases": ["角膜", "cornea", "corneal"], "targets": ["cornea"], "display": "Cornea"},
        {"aliases": ["牙龈", "gingiva", "gingival"], "targets": ["gingiva"], "display": "Gingiva"},
        {"aliases": ["口腔", "口腔黏膜", "颊黏膜", "oral cavity", "buccal"],
         "targets": ["oral cavity", "buccal", "mouth"], "display": "Oral Cavity"},
        {"aliases": ["舌头", "舌", "tongue"], "targets": ["tongue"], "display": "Tongue"},
        {"aliases": ["膈肌", "横膈", "diaphragm"], "targets": ["diaphragm"], "display": "Diaphragm"},
        {"aliases": ["腹膜", "腹腔", "peritoneum", "peritoneal"], "targets": ["peritone"], "display": "Peritoneum"},
        {"aliases": ["胸膜", "胸水", "胸腔积液", "pleura", "pleural"], "targets": ["pleura"], "display": "Pleura"},
        {"aliases": ["脐带", "脐带血", "umbilical", "umbilical cord"], "targets": ["umbilical"], "display": "Umbilical Cord"},
        {"aliases": ["输卵管", "fallopian", "fallopian tube", "oviduct"], "targets": ["fallopian", "oviduct"], "display": "Fallopian Tube"},
        # 「子宫内膜」此前被登记成 disease=Endometrial Cancer —— 维度归属反了：它是**组织**，
        # 语料里 endometrium 组织 22 条、endometrial 癌 5 条。查子宫内膜的人多数要的是组织。
        # 拆开：组织归这里，癌症在 disease 侧改名为「子宫内膜癌」。
        {"aliases": ["子宫内膜", "endometrium"], "targets": ["endometrium"], "display": "Endometrium"},
        {"aliases": ["牙齿", "牙髓", "牙", "tooth", "dental pulp"], "targets": ["tooth", "dental"], "display": "Tooth"},
        {"aliases": ["软骨", "cartilage"], "targets": ["cartilage"], "display": "Cartilage"},
        {"aliases": ["关节", "滑膜", "synovium", "synovial"], "targets": ["synovi", "joint"], "display": "Joint/Synovium"},
        {"aliases": ["血管", "动脉", "静脉", "blood vessel", "vasculature", "artery"],
         "targets": ["blood vessel", "vasculature", "artery", "vein"], "display": "Blood Vessel"},
        {"aliases": ["空肠", "jejunum", "jejunal"], "targets": ["jejunum"], "display": "Jejunum"},
        {"aliases": ["盲肠", "caecum", "cecum"], "targets": ["caecum", "cecum"], "display": "Caecum"},
        {"aliases": ["阑尾", "appendix"], "targets": ["appendix"], "display": "Appendix"},
        # 只收「皮层 / 大脑皮层 / 大脑皮质」，**不收裸「皮质」**——「皮质醇」含它，会把内分泌查询
        # 劫持成组织=Cortex。四字形不与任何常见词重叠。
        {"aliases": ["大脑皮层", "大脑皮质", "皮层", "cortex", "cortical"], "targets": ["cortex"], "display": "Cortex"},
        {"aliases": ["唾液腺", "涎腺", "salivary", "salivary gland"], "targets": ["salivary"], "display": "Salivary Gland"},
        {"aliases": ["胆管", "胆道", "bile duct", "cholangiocyte"], "targets": ["bile duct", "cholangi"], "display": "Bile Duct"},
        {"aliases": ["中脑", "midbrain"], "targets": ["midbrain"], "display": "Midbrain"},
        {"aliases": ["脑干", "后脑", "hindbrain", "brainstem", "brain stem"], "targets": ["hindbrain", "brainstem"], "display": "Brainstem"},
        {"aliases": ["鼻腔", "鼻", "nasal", "nose"], "targets": ["nasal", "nose"], "display": "Nose"},
        {"aliases": ["咽部", "咽", "pharynx", "pharyngeal"], "targets": ["pharyn"], "display": "Pharynx"},
        {"aliases": ["喉部", "喉", "larynx", "laryngeal"], "targets": ["laryn"], "display": "Larynx"},
        {"aliases": ["附睾", "epididymis"], "targets": ["epididymis"], "display": "Epididymis"},
    ],
    "disease": [
        {"aliases": ["乳腺癌", "breast cancer", "idc", "invasive ductal carcinoma", "ductal carcinoma", "tnbc"],
         "targets": ["breast cancer", "invasive ductal carcinoma", "ductal carcinoma"], "display": "Breast Cancer"},
        {"aliases": ["肺癌", "lung cancer", "nsclc", "non-small cell lung", "非小细胞肺癌"],
         "targets": ["lung cancer", "non-small cell lung"], "display": "Lung Cancer"},
        {"aliases": ["肝癌", "liver cancer", "hcc", "hepatocellular"],
         "targets": ["hepatocellular", "liver cancer"], "display": "Liver Cancer"},
        # 「非霍奇金」词首「非」是词素、非排除（此前误报 unsupported_negation）。
        # 不收 3 字裸缩写 "nhl"：会子串误命中域内知名机构名 NHLBI（→静默注入 disease=lymphoma），全称/中文形已足够。
        # 霍奇金亚型拆出独立条目（见下）——此前「霍奇金淋巴瘤」泛化到 lymphoma，
        # base 里 rank-1 被 small lymphocytic lymphoma（非霍奇金亚型）抢走，是 holdout 首跑唯一硬违规的问题。
        # 「非霍奇金」各形必须留在本条（泛称）：它们比霍奇金条目的对应别名长（非霍奇金淋巴瘤>霍奇金淋巴瘤、
        # non-hodgkin lymphoma>hodgkin lymphoma、non-hodgkin>hodgkin），最长优先消费保证它们到不了霍奇金条目。
        {"aliases": ["淋巴瘤", "lymphoma", "非霍奇金淋巴瘤", "非霍奇金",
                     "non-hodgkin lymphoma", "non-hodgkin"],
         "targets": ["lymphoma"], "display": "Lymphoma"},
        # 霍奇金淋巴瘤（dv34）：base 7 条均为 "Hodgkin's Lymphoma"。target "hodgkin" 是子串判定，
        # 已知局限：若库中混入 "non-hodgkin ..." 写法也会被它兜进（裁判同口径，见 dev 集 dv34 _note）。
        {"aliases": ["霍奇金淋巴瘤", "霍奇金", "hodgkin lymphoma", "hodgkin's lymphoma", "hodgkin"],
         "targets": ["hodgkin"], "display": "Hodgkin's Lymphoma"},
        # 小淋巴细胞淋巴瘤（dv35）：非霍奇金系亚型，base 2 条 "small lymphocytic lymphoma"。
        {"aliases": ["小淋巴细胞淋巴瘤", "small lymphocytic lymphoma"],
         "targets": ["small lymphocytic"], "display": "Small Lymphocytic Lymphoma"},
        {"aliases": ["黑色素瘤", "melanoma"], "targets": ["melanoma"], "display": "Melanoma"},
        {"aliases": ["胶质母细胞瘤", "胶质瘤", "glioblastoma", "glioma", "gbm"], "targets": ["glioblastoma", "glioma"], "display": "Glioblastoma"},
        {"aliases": ["胃癌", "gastric cancer", "gastric adenocarcinoma"], "targets": ["gastric"], "display": "Gastric Cancer"},
        # aml / acute myeloid 从泛称条目移入下方急髓专条——
        # 此前「急性髓系白血病」落到泛称 leukemia，base 里 rank 被其他白血病亚型抢走（dv37 Top1/Top5 双失）。
        {"aliases": ["白血病", "leukemia", "leukaemia", "cll"], "targets": ["leukemia"], "display": "Leukemia"},
        # 急性髓系白血病（dv37，急髓/AML）：base 4 条 "Acute myeloid leukemia (AML)"。
        {"aliases": ["急性髓系白血病", "急性髓细胞白血病", "急性粒细胞白血病", "急髓",
                     "acute myeloid leukemia", "acute myeloid", "aml"],
         "targets": ["acute myeloid"], "display": "Acute Myeloid Leukemia"},
        # 急性淋巴细胞白血病（dv38，急淋/ALL）：base 1 条写作 "acute lymphoid leukemia"，
        # 故 target 同收 lymphoblastic/lymphoid 两形（子串「或」匹配）。
        {"aliases": ["急性淋巴细胞白血病", "急性淋巴母细胞白血病", "急淋",
                     "acute lymphoblastic leukemia", "acute lymphoblastic",
                     "acute lymphoid leukemia", "acute lymphoid"],
         "targets": ["acute lymphoblastic", "acute lymphoid"], "display": "Acute Lymphoblastic/Lymphoid Leukemia"},
        {"aliases": ["卵巢癌", "ovarian cancer", "ovarian carcinoma"], "targets": ["ovarian"], "display": "Ovarian Cancer"},
        {"aliases": ["宫颈癌", "cervical cancer"], "targets": ["cervical"], "display": "Cervical Cancer"},
        {"aliases": ["结直肠癌", "结肠癌", "colorectal cancer", "crc"], "targets": ["colorectal", "crc"], "display": "Colorectal Cancer"},
        # 改名：原别名是裸「子宫内膜」，把**组织**说法吞进了癌症维度。
        # 现在只认明确的癌症说法；组织侧「子宫内膜」见 tissue。
        {"aliases": ["子宫内膜癌", "endometrial cancer", "endometrial carcinoma", "endometrial adenocarcinoma"],
         "targets": ["endometrial"], "display": "Endometrial Cancer"},
        {"aliases": ["腺癌", "adenocarcinoma"], "targets": ["adenocarcinoma"], "display": "Adenocarcinoma"},
        {"aliases": ["鳞癌", "鳞状细胞癌", "squamous"], "targets": ["squamous"], "display": "Squamous Carcinoma"},
        {"aliases": ["健康", "正常", "healthy", "normal"], "targets": ["healthy", "normal"], "display": "Healthy/Normal"},
        # 外部平台库常见的非癌疾病（神经退行/代谢/心血管/自免/感染/呼吸；基础库以癌症为主 → 补齐提召回）
        {"aliases": ["新冠", "新冠肺炎", "新型冠状病毒", "covid", "covid-19", "sars-cov-2"], "targets": ["covid"], "display": "COVID-19"},
        {"aliases": ["阿尔茨海默", "阿尔兹海默", "老年痴呆", "alzheimer", "alzheimer's"], "targets": ["alzheimer"], "display": "Alzheimer's Disease"},
        {"aliases": ["痴呆", "失智", "dementia"], "targets": ["dementia"], "display": "Dementia"},
        {"aliases": ["帕金森", "parkinson", "parkinson's"], "targets": ["parkinson"], "display": "Parkinson's Disease"},
        {"aliases": ["多发性硬化", "multiple sclerosis"], "targets": ["multiple sclerosis"], "display": "Multiple Sclerosis"},
        {"aliases": ["肌萎缩侧索硬化", "渐冻症", "amyotrophic lateral sclerosis", "amyotrophic"], "targets": ["amyotrophic"], "display": "ALS"},
        {"aliases": ["癫痫", "epilepsy", "epileptic"], "targets": ["epilepsy"], "display": "Epilepsy"},
        {"aliases": ["糖尿病", "diabetes", "diabetic"], "targets": ["diabet"], "display": "Diabetes"},
        {"aliases": ["心肌梗死", "心梗", "myocardial infarction"], "targets": ["myocardial infarction", "infarction"], "display": "Myocardial Infarction"},
        {"aliases": ["心力衰竭", "心衰", "heart failure"], "targets": ["heart failure"], "display": "Heart Failure"},
        {"aliases": ["动脉粥样硬化", "atherosclerosis", "atherosclerotic"], "targets": ["atherosclero"], "display": "Atherosclerosis"},
        {"aliases": ["克罗恩", "克罗恩病", "crohn", "crohn's"], "targets": ["crohn"], "display": "Crohn's Disease"},
        {"aliases": ["溃疡性结肠炎", "ulcerative colitis"], "targets": ["ulcerative colitis"], "display": "Ulcerative Colitis"},
        {"aliases": ["炎症性肠病", "inflammatory bowel disease", "ibd"], "targets": ["inflammatory bowel"], "display": "IBD"},
        {"aliases": ["慢阻肺", "慢性阻塞性肺", "chronic obstructive pulmonary", "copd"], "targets": ["chronic obstructive"], "display": "COPD"},
        {"aliases": ["哮喘", "asthma", "asthmatic"], "targets": ["asthma"], "display": "Asthma"},
        {"aliases": ["肺纤维化", "肺间质纤维化", "间质性肺", "pulmonary fibrosis", "interstitial lung"], "targets": ["pulmonary fibrosis", "interstitial lung"], "display": "Pulmonary Fibrosis"},
        {"aliases": ["囊性纤维化", "cystic fibrosis"], "targets": ["cystic fibrosis"], "display": "Cystic Fibrosis"},
        {"aliases": ["银屑病", "牛皮癣", "psoriasis", "psoriatic"], "targets": ["psoriasis", "psoriatic"], "display": "Psoriasis"},
        {"aliases": ["神经母细胞瘤", "neuroblastoma"], "targets": ["neuroblastoma"], "display": "Neuroblastoma"},
        # ── 语料实证覆盖扩充（疾病）──────────────────────────────
        # 除了补覆盖，这一批还顺手修掉一整类**静默错筛**：中文别名是裸子串匹配，短别名会被更长的
        # 不同概念词包含，于是「高血压」命中「血」→ 组织=Blood、「骨髓瘤」命中「骨髓」→ 组织=Bone Marrow、
        # 「肺炎/肾炎/肝炎/胃炎/肾病」各自退化成对应器官。界面上会挂一个看起来很正常的
        # 「组织：Blood」标签，用户根本不会怀疑——比零返回更危险。
        # 修法不是加特例，而是**把这些概念本身登记成疾病**：alias 消费是全局长度降序，
        # 3 字的「高血压」天然压过 1 字的「血」，病根自动消失（和「肺癌」压过「肺」是同一个机制）。
        {"aliases": ["纤维化", "fibrosis", "fibrotic"], "targets": ["fibrosis"], "display": "Fibrosis"},
        {"aliases": ["多囊肾病", "多囊肾", "polycystic kidney"], "targets": ["polycystic kidney"], "display": "Polycystic Kidney Disease"},
        {"aliases": ["扩张型心肌病", "肥厚型心肌病", "心肌病", "cardiomyopathy"], "targets": ["cardiomyopathy"], "display": "Cardiomyopathy"},
        {"aliases": ["心肌炎", "myocarditis"], "targets": ["myocarditis"], "display": "Myocarditis"},
        {"aliases": ["高血压", "hypertension", "hypertensive"], "targets": ["hypertens"], "display": "Hypertension"},
        {"aliases": ["肥胖", "obesity", "obese"], "targets": ["obes"], "display": "Obesity"},
        {"aliases": ["非酒精性脂肪肝", "脂肪肝", "fatty liver", "nafld", "steatohepatitis"],
         "targets": ["fatty liver", "steatohepatitis"], "display": "Fatty Liver Disease"},
        {"aliases": ["肝硬化", "cirrhosis", "cirrhotic"], "targets": ["cirrhosis"], "display": "Cirrhosis"},
        {"aliases": ["肝炎", "hepatitis"], "targets": ["hepatitis"], "display": "Hepatitis"},
        {"aliases": ["胰腺癌", "pancreatic cancer", "pancreatic ductal adenocarcinoma", "pdac"],
         "targets": ["pancreatic cancer", "pancreatic ductal"], "display": "Pancreatic Cancer"},
        {"aliases": ["肾细胞癌", "肾癌", "renal cell carcinoma", "kidney cancer"],
         "targets": ["renal cell", "kidney cancer"], "display": "Renal Cell Carcinoma"},
        {"aliases": ["膀胱癌", "尿路上皮癌", "bladder cancer", "urothelial carcinoma"],
         "targets": ["bladder cancer", "urothelial"], "display": "Bladder Cancer"},
        {"aliases": ["甲状腺癌", "thyroid cancer", "thyroid carcinoma"],
         "targets": ["thyroid cancer", "thyroid carcinoma"], "display": "Thyroid Cancer"},
        {"aliases": ["多发性骨髓瘤", "骨髓瘤", "multiple myeloma", "myeloma"], "targets": ["myeloma"], "display": "Myeloma"},
        {"aliases": ["骨肉瘤", "肉瘤", "osteosarcoma", "sarcoma"], "targets": ["sarcoma"], "display": "Sarcoma"},
        {"aliases": ["髓母细胞瘤", "medulloblastoma"], "targets": ["medulloblastoma"], "display": "Medulloblastoma"},
        {"aliases": ["视网膜母细胞瘤", "retinoblastoma"], "targets": ["retinoblastoma"], "display": "Retinoblastoma"},
        {"aliases": ["间皮瘤", "mesothelioma"], "targets": ["mesothelioma"], "display": "Mesothelioma"},
        {"aliases": ["类风湿关节炎", "类风湿", "rheumatoid"], "targets": ["rheumatoid"], "display": "Rheumatoid Arthritis"},
        {"aliases": ["骨关节炎", "osteoarthritis"], "targets": ["osteoarthritis"], "display": "Osteoarthritis"},
        {"aliases": ["关节炎", "arthritis"], "targets": ["arthritis"], "display": "Arthritis"},
        {"aliases": ["系统性红斑狼疮", "红斑狼疮", "狼疮", "lupus"], "targets": ["lupus"], "display": "Lupus"},
        {"aliases": ["干燥综合征", "舍格伦综合征", "sjogren"], "targets": ["sjogren"], "display": "Sjogren Syndrome"},
        {"aliases": ["肺结核", "结核", "tuberculosis"], "targets": ["tuberculosis"], "display": "Tuberculosis"},
        {"aliases": ["艾滋病", "hiv"], "targets": ["hiv"], "display": "HIV"},
        {"aliases": ["流行性感冒", "流感", "influenza"], "targets": ["influenza"], "display": "Influenza"},
        {"aliases": ["脓毒症", "败血症", "sepsis", "septic"], "targets": ["sepsis"], "display": "Sepsis"},
        {"aliases": ["唐氏综合征", "down syndrome"], "targets": ["down syndrome"], "display": "Down Syndrome"},
        # 「肌营养不良」里的「不良」曾让整句报 unsupported_negation（「不」被当排除操作符）。
        # 登记成实体后，_entity_spans 会保护 span 内的否定字，这条误报随之消失。
        {"aliases": ["杜氏肌营养不良", "肌营养不良", "muscular dystrophy"], "targets": ["dystrophy"], "display": "Muscular Dystrophy"},
        {"aliases": ["青光眼", "glaucoma"], "targets": ["glaucoma"], "display": "Glaucoma"},
        {"aliases": ["老年黄斑变性", "黄斑变性", "macular degeneration"], "targets": ["macular degeneration"], "display": "Macular Degeneration"},
        {"aliases": ["白内障", "cataract"], "targets": ["cataract"], "display": "Cataract"},
        {"aliases": ["过敏", "变态反应", "allergy", "allergic", "hypersensitivity"],
         "targets": ["allerg", "hypersensitiv"], "display": "Allergy"},
        {"aliases": ["特应性皮炎", "湿疹", "皮炎", "dermatitis", "eczema"],
         "targets": ["dermatitis", "eczema"], "display": "Dermatitis/Eczema"},
        {"aliases": ["脊髓损伤", "spinal cord injury"], "targets": ["spinal cord injury"], "display": "Spinal Cord Injury"},
        {"aliases": ["脑卒中", "脑梗死", "中风", "卒中", "脑梗", "stroke"], "targets": ["stroke"], "display": "Stroke"},
        {"aliases": ["先天性心脏病", "先心病", "congenital heart"], "targets": ["congenital heart"], "display": "Congenital Heart Disease"},
        {"aliases": ["胃炎", "gastritis"], "targets": ["gastritis"], "display": "Gastritis"},
        {"aliases": ["巨细胞病毒", "cytomegalovirus", "cmv"], "targets": ["cytomegalovirus"], "display": "Cytomegalovirus Infection"},
        {"aliases": ["急性肾损伤", "acute kidney injury"], "targets": ["acute kidney injury"], "display": "Acute Kidney Injury"},
        {"aliases": ["慢性肾脏病", "慢性肾病", "chronic kidney disease"], "targets": ["chronic kidney disease"], "display": "Chronic Kidney Disease"},
        {"aliases": ["肾小球肾炎", "肾病", "肾炎", "nephropathy", "nephritis", "glomerulonephritis"],
         "targets": ["nephropathy", "nephritis"], "display": "Nephropathy/Nephritis"},
        {"aliases": ["肺炎", "pneumonia"], "targets": ["pneumonia"], "display": "Pneumonia"},
        # 回归验证：「血管瘤」此前命中裸「血管」→ 组织=Blood Vessel/Artery/Vein，
        # 用户搜血管瘤，拿到 11 条血管组织数据、没有一条是血管瘤。语料里 hemangioma 确有 1 条
        #（normal, liver hemangioma），所以这里**登记本体**而不是塞进保护表——保护表会让它
        # 弃权说「系统未收录」，而库里明明有，那是另一种撒谎。
        {"aliases": ["血管瘤", "肝血管瘤", "hemangioma"], "targets": ["hemangioma"], "display": "Hemangioma"},
        # 非整倍体：GEO 试点切片标志性条件词（GSE3642 单细胞 array-CGH 非整倍体
        # 检测），SCP 亦有 1 条；此前整句 unresolved_term 弃权。染色体拷贝数异常状态、非癌，
        # 独立成条；alias「aneuploid」兼形容词形，中文「非整倍体」是四字专用术语、无碰撞。
        # 提升后 GEO/SCP 各有 1 条记录 disease 字段含该词（反标落笔）→ 不需要 absent_ok。
        {"aliases": ["非整倍体", "aneuploidy", "aneuploid"], "targets": ["aneuploid"], "display": "Aneuploidy"},
        {"aliases": ["巴雷特食管", "barrett"], "targets": ["barrett"], "display": "Barrett Esophagus"},
        {"aliases": ["头颈部鳞癌", "头颈鳞癌", "head and neck"], "targets": ["head and neck"], "display": "Head and Neck Carcinoma"},
        {"aliases": ["周围神经病", "神经病变", "neuropathy"], "targets": ["neuropathy"], "display": "Neuropathy"},
        {"aliases": ["主动脉狭窄", "aortic stenosis"], "targets": ["aortic stenosis"], "display": "Aortic Stenosis"},
        {"aliases": ["心房颤动", "房颤", "atrial fibrillation"], "targets": ["atrial fibrillation"], "display": "Atrial Fibrillation"},
        {"aliases": ["贫血", "anemia", "anaemia"], "targets": ["anemia", "anaemia"], "display": "Anemia"},
        {"aliases": ["深静脉血栓", "血栓", "thrombosis"], "targets": ["thrombosis"], "display": "Thrombosis"},
        {"aliases": ["肺气肿", "emphysema"], "targets": ["emphysema"], "display": "Emphysema"},
        {"aliases": ["脑出血", "出血", "hemorrhage", "haemorrhage"], "targets": ["hemorrhage", "haemorrhage"], "display": "Hemorrhage"},
        {"aliases": ["癌", "肿瘤", "cancer", "tumor", "tumour", "carcinoma", "neoplasm"],
         "targets": ["cancer", "carcinoma"], "display": "Cancer (generic)", "generic": True},
    ],
    # 平台家族：匹配 record.platform_family
    "platform": [
        {"aliases": ["空间转录组", "空间", "visium", "spatial transcriptomics", "spatial"], "targets": ["visium"], "display": "Visium"},
        {"aliases": ["xenium", "原位", "in situ"], "targets": ["xenium"], "display": "Xenium"},
        {"aliases": ["chromium"], "targets": ["chromium"], "display": "Chromium (platform)"},
        {"aliases": ["atera"], "targets": ["atera"], "display": "Atera", "absent_ok": True},
    ],
    # 数据模态：解离式单细胞 vs 空间/原位。`单细胞`不再等同某一平台（见 derive_modality）。
    # 匹配 record.modality（single-cell / spatial / ""）。空间侧别名暂不建 modality（空间/原位仍 pin
    # platform=visium/xenium，是多条冻结题的载荷；加 spatial modality 须原子重基线那些题，另议）。
    "modality": [
        # scrnaseq/snrnaseq：既有 scrna-seq/snrna-seq 的无连字符写法，
        # SCP/GEO 描述里实测 28+8 条用此形；8 字符专写、无碰撞。右侧 s 复数容忍规则
        # 救不了它（'scrnaseq' 里 scrna 后紧跟 e 不是词尾 s），必须显式登记。
        {"aliases": ["单细胞", "单细胞测序", "单细胞转录组", "单核", "单核测序",
                     "single cell", "single-cell", "single nucleus", "single-nucleus",
                     "snrna", "snrna-seq", "scrna", "scrna-seq", "sc-rna-seq",
                     "scrnaseq", "snrnaseq"],
         "targets": ["single-cell"], "display": "Single-cell"},
    ],
    # 实验类型：匹配 record.assay
    "assay": [
        # scatac/snatac 等前缀写法过去靠「裸子串命中 atac」偶然生效；alias 改按词边界匹配后
        # （见 query_parser._alias_occurrences）必须**显式登记**。依赖偶然包含本来就是这次
        # 「integrated 里的 rat」事故的同一个病根，这里一并写清楚。
        {"aliases": ["atac", "scatac", "sc-atac", "snatac", "sn-atac", "atac-seq",
                     "染色质", "可及性", "chromatin accessibility"], "targets": ["atac"], "display": "ATAC"},
        {"aliases": ["multiome", "多组学"], "targets": ["multiome"], "display": "Multiome"},
        {"aliases": ["flex", "固定rna", "fixed rna"], "targets": ["flex"], "display": "Flex"},
        {"aliases": ["cnv", "拷贝数"], "targets": ["cnv"], "display": "CNV"},
        # ① 补「V(D)J」带括号形、「免疫受体（库）」、「TCR/BCR」别名——
        # 此前 "vdj" 裸形匹配不到用户常写的 "V(D)J"，「免疫受体库」未收录，均整句弃权；
        # ② target 增列 "v(d)j"：base 774 条里 12 条 V(D)J 记录的 chemistry 写作 "…Single Cell V(D)J v1.1"，
        # 没有任何记录的 assay/chemistry 含 "immune"——旧单 target "immune" 在 base 上零命中，
        # 「VDJ 数据」即便解析成功也查不到一条（dv23/dv26 修复前 n_returned=0）。targets 为「任一」语义，两形并存。
        {"aliases": ["免疫组库", "免疫受体库", "免疫受体", "immune profiling", "immune repertoire",
                     "vdj", "v(d)j", "v（d）j", "tcr", "bcr"],
         "targets": ["immune", "v(d)j"], "display": "Immune Profiling"},
        # 非 10x 单细胞/空间平台（外部库常见）：匹配 assay+chemistry 子串。别名均≥6字且带连字符、
        # 高辨识度，不会误命中普通查询词；基础库无此类 chemistry，故冻结门(base-only)不受影响。
        {"aliases": ["smart-seq", "smart-seq2", "smartseq", "smartseq2", "smart-like"], "targets": ["smart"], "display": "Smart-seq"},
        {"aliases": ["drop-seq", "dropseq"], "targets": ["drop-seq"], "display": "Drop-seq"},
        {"aliases": ["slide-seq", "slide-seqv2", "slideseq"], "targets": ["slide-seq"], "display": "Slide-seq"},
        {"aliases": ["sci-rna-seq", "sci-rna-seq3"], "targets": ["sci-rna-seq"], "display": "sci-RNA-seq"},
        {"aliases": ["merfish"], "targets": ["merfish"], "display": "MERFISH"},
        {"aliases": ["seq-well", "seqwell"], "targets": ["seq-well"], "display": "Seq-Well"},
        {"aliases": ["cite-seq", "citeseq"], "targets": ["cite-seq"], "display": "CITE-seq"},
        # 此前一律 unresolved_term 弃权的单细胞/空间技术专名。
        # 全部 ASCII、≥5 字符、高辨识度，不会误命中普通中文查询。语料里没有的 target 也照收——
        # 词表设计原则就是「收录库中不存在的已知实体」，好让解析器看见约束后诚实返回无结果，
        # 而不是把整句判成看不懂。
        {"aliases": ["indrop", "in-drop"], "targets": ["indrop"], "display": "inDrop", "absent_ok": True},
        {"aliases": ["bd rhapsody", "rhapsody"], "targets": ["rhapsody"], "display": "BD Rhapsody", "absent_ok": True},
        {"aliases": ["evercode", "parse biosciences"], "targets": ["evercode"], "display": "Parse Evercode", "absent_ok": True},
        {"aliases": ["stereo-seq", "stereoseq"], "targets": ["stereo-seq"], "display": "Stereo-seq", "absent_ok": True},
        {"aliases": ["cosmx"], "targets": ["cosmx"], "display": "CosMx", "absent_ok": True},
        {"aliases": ["geomx"], "targets": ["geomx"], "display": "GeoMx", "absent_ok": True},
        {"aliases": ["fluidigm c1", "fluidigm"], "targets": ["fluidigm"], "display": "Fluidigm C1", "absent_ok": True},
        {"aliases": ["mars-seq", "marsseq"], "targets": ["mars-seq"], "display": "MARS-seq", "absent_ok": True},
        {"aliases": ["strt-seq"], "targets": ["strt-seq"], "display": "STRT-seq", "absent_ok": True},
        {"aliases": ["split-seq", "splitseq"], "targets": ["split-seq"], "display": "Split-seq", "absent_ok": True},
        {"aliases": ["cellplex"], "targets": ["cellplex"], "display": "CellPlex", "absent_ok": True},
        # HuBMAP chemistry 高频的空间/多重成像技术专名。
        # ASCII 别名有词边界保护（query_parser._alias_occurrences），不收 Resolve（普通英文动词，
        # 误命中风险大于收益，该家族记录量也极小）。10X Multiome 已被既有 multiome 条目的子串覆盖。
        {"aliases": ["codex"], "targets": ["codex"], "display": "CODEX", "absent_ok": True},
        {"aliases": ["mibi"], "targets": ["mibi"], "display": "MIBI", "absent_ok": True},
        {"aliases": ["phenocycler"], "targets": ["phenocycler"], "display": "PhenoCycler", "absent_ok": True},
        {"aliases": ["seqfish"], "targets": ["seqfish"], "display": "seqFISH", "absent_ok": True},
        {"aliases": ["snare-seq", "snare-seq2", "snareseq2"], "targets": ["snare-seq"], "display": "SNARE-seq2", "absent_ok": True},
        {"aliases": ["cell dive"], "targets": ["cell dive"], "display": "Cell DIVE", "absent_ok": True},
        {"aliases": ["dbit"], "targets": ["dbit"], "display": "DBiT", "absent_ok": True},
        {"aliases": ["hifi-slide", "hifislide"], "targets": ["hifi-slide"], "display": "HiFi-Slide", "absent_ok": True},
        {"aliases": ["pixel-seq", "pixel-seqv2", "pixelseq"], "targets": ["pixel-seq"], "display": "Pixel-seqV2", "absent_ok": True},
        {"aliases": ["molecular cartography"], "targets": ["molecular cartography"], "display": "Molecular Cartography", "absent_ok": True},
        # ── SCP/GEO NL 检索补盲：两源 description 里验证高频、此前一律
        # unresolved_term 弃权的单细胞方法学专名（词边界计数：perturb-seq 14 / snuc-seq 9 /
        # dronc-seq 4 / pick-seq 4 / div-seq 2 条记录）。全部 ≥6 字符带连字符（无连字符变体
        # ≥7 字符），ASCII 词边界保护下不与普通英文词碰撞；只收连字/全称形，不收裸词根
        # （div/pick/snuc 裸形太泛，会误命中日常英语）。target 即方法名子串：反标富化会把
        # display 写进 chemistry 字段（corpus_enrich），硬过滤按 assay+chemistry 子串命中，
        # 故提升后 target 在语料字段中真实存在——不需要 absent_ok（该旗标只是文档性标注，
        # 供「库里可能真没有」的条目用，本条目不属此类）。
        {"aliases": ["perturb-seq", "perturbseq"], "targets": ["perturb-seq"], "display": "Perturb-seq"},
        {"aliases": ["snuc-seq", "snucseq"], "targets": ["snuc-seq"], "display": "sNuc-Seq"},
        {"aliases": ["div-seq"], "targets": ["div-seq"], "display": "Div-Seq"},
        {"aliases": ["dronc-seq"], "targets": ["dronc-seq"], "display": "DRonC-seq"},
        {"aliases": ["pick-seq"], "targets": ["pick-seq"], "display": "Pick-seq"},
    ],
}

# raw_data / FASTQ 触发词
RAW_REQUIRED_ALIASES = [
    "fastq", "原始数据", "有原始数据", "raw data", "from fastq",
    "可重新跑", "重新跑流程", "重新比对", "重新分析", "可复现", "重跑",
    # 「要原始文件 / 原始序列」此前不是 raw 说法，「原始」两字落进残差门 →
    # 整句 unresolved_term 弃权。补齐同义写法；注意必须**收进 raw 别名**而不是丢进 filler，
    # 否则「要原始文件」会变成静默不筛 raw（用户明确要了原始数据却没筛，比弃权更糟）。
    "原始文件", "原始序列", "原始测序数据", "原始 fastq", "原始fastq",
    "raw file", "raw files", "raw fastq", "raw sequencing",
]
RAW_NOT_REQUIRED_ALIASES = ["无需fastq", "不需要fastq", "不需要原始数据", "不要原始数据", "无需原始数据"]

# ---------- fail-closed 残差门：停用词/填充词 ----------
# 命中词表后，从查询里去掉这些 + 已匹配 alias，若仍剩≥2 连续中文/未知实体词 => abstain。
#
# （静默丢词诚实层）：把原单表 FILLER_TOKENS 拆成两组，语义不同、用途不同。
# **FILLER_TOKENS = FILLER_GRAMMAR + FILLER_DOMAIN 的并集**，成员逐位不变 → _residual_salient /
# 残差门 / 弃权阈值 / 冻结 767 全部行为不变。拆分只为让「静默丢词」诚实层能区分该不该回显。
#
#   · FILLER_GRAMMAR：语法/客套/元词（推荐/帮我/的/数据集…）+ **领域通用头词**（组织/平台/细胞/
#     转录组/图谱/基因/序列…）。这些**不回显**——要么是纯噪声，要么是某个已落维度的通用头：
#     「肺组织」里 tissue 已由「肺」落维、「组织」只是残留头词，若报「组织未作筛选」就是**撒谎**
#     （正是诚实层要消灭的静默判负的镜像）。
#   · FILLER_DOMAIN：性别/年龄/发育阶段/受试者类型/功能类等 **系统结构上没有对应筛选维度** 的实义
#     描述词。用户很可能把它当约束输入，但它既不落维（无此维度）、又不入 free_text_terms（ASCII-only
#     正则抓不到中文）、还因在 filler 表里而不触发 unresolved_term 弃权 → **静默丢弃、零信号**。
#     诚实层据此回显 unused_query_terms「以下词未作为筛选维度」。按定义这些词都**不是任何维度的头词**，
#     故回显不存在「头/值」歧义、不会误报已落维度。
# ---------- 执行类说法：不是检索条件，但也不该炸掉检索 ----------
# 验证：「人类肺数据，帮我打包前20条」「人类肺癌数据，生成下载脚本」「导出引文」——
# 这些说法**每一句都整句弃权**，连人类肺数据都查不到。用户想的是「检索完顺手打包」，
# 得到的却是「查询里有系统未收录的词：打包前」。
#
# 处理分两半，缺一不可：
#   · 并入 FILLER_GRAMMAR（见其后的程序化合并）→ 检索不再被它们炸掉；
#   · 但**不能就这么算了**——静默吞掉用户明确说出的「打包」是本项目反复修过的那类错。
#     所以另出一个只读的 `query_parser.detect_action_markers` 回显，把「你说了打包，功能在这儿」讲清楚。
#     这是「一句话检索 + 下载」那条想法的**确定性半边**：识别意图、指路，但**不替用户执行**。
#
# 拆表：**动作**与**对象**必须分开，因为它们承担的职责不同。
#   · 动作词（ACTION_VERBS）表示「去做一件事」，是**路由依据**：据它把一句话交给任务包那条路。
#   · 对象词（ACTION_NOUNS）是产物的名字，不是动作。它们仍然进填充词表（不炸检索）、仍然参与回显，
#     但**不能拿来路由**——实测这 5 句都被裸子串劫持成「打开打包面板」，用户那半句真实诉求当场蒸发：
#         「去掉批量效应大的」   →「批量」
#         「只保留能下载的」      →「下载」（这句是动作词，只能靠上层语境救，见下）
#         「候选清单里去掉重复的」→「清单」
#     劫持比零返回更糟：屏幕上确实弹出了一个面板，看起来「有反应」，但办的根本不是他要的事。
ACTION_VERBS = (
    "打包", "打个包", "生成包", "做成包", "整理成",
    "下载脚本", "下载链接", "批量下载", "下载", "拉取", "抓取",
    "导出", "download script", "export",
)

ACTION_NOUNS = (
    "引文", "参考文献", "引用格式", "清单", "列表文件", "文件清单",
    "脚本", "批量", "manifest", "citation",
)

#: 回显与填充词表用**并集**——「你说了打包/引文」这类如实回音不该因为拆表而少掉一半。
#: 程序并，不手抄（本仓库在两份条件板投影上栽过多次）。
ACTION_MARKERS = tuple(dict.fromkeys(ACTION_VERBS + ACTION_NOUNS))

#: 管护类操作短语：「AI 执行」关闭时路由层的**规则**降级检测用
#: （turn.route_turn 的 agent_off 分支——检出后不执行、只回降级气泡指路「设置 → AI 执行」）。
#: 收录口径与 ACTION_VERBS 同源：**操作短语**，不收可能出现在检索句里的裸词
#: （如「恢复」可能是疾病康复、「状态」可能是样本状态，都不收）。这些词炸不掉检索——
#: 命中后的处置只是一颗提示气泡，真正的检索交给用户改写说法或开启 AI 执行。
CURATE_OP_MARKERS = (
    "导入", "上传",
    "删除", "删掉", "回收站", "撤销删除", "找回",
    "联网搜", "在线搜", "上网搜", "网上搜",
    "检查更新", "有没有更新", "是否有更新", "有更新吗",
    "清点", "汇报",
)

FILLER_GRAMMAR = [
    "推荐", "帮我", "给我", "我想", "想要", "需要", "找", "查", "搜索", "搜", "检索",
    "一些", "一点", "点", "些", "的", "了", "吗", "呢", "吧", "呀", "啊", "和", "与",
    "数据", "数据集", "数据库", "样本", "样品", "相关", "关于", "看看", "看", "有关",
    "请", "麻烦", "谢谢", "以及", "还有", "这", "那", "个", "条", "份",
    "方面", "类", "type", "dataset", "datasets", "data", "please", "show", "me",
    "the", "a", "an", "of", "for", "with", "give", "find", "want", "some", "about", "recommend",
    # 领域通用头词（非约束，且常是已落维度的通用头 → 不回显）
    "组织", "平台", "可以", "测序", "转录组", "图谱", "细胞", "基因", "表达",
    "项目", "实验", "分析", "结果", "文件", "序列", "谱", "库", "技术", "方案",
    # 注意：「没有」曾在此当 filler → 会把「没有小鼠」静默吞成正向 mouse（反向 bug）。
    # 现由否定语法识别（EXEC_NEG_PREFIX_CN 的「没有/没」），故从 filler 移除；存在性问句「有没有」另由
    # NEG_EXISTENTIAL_PHRASES 整体保护，不落入否定分支。
    "是", "有", "的话", "这个", "那个", "我", "你",
    # 修饰/限定词（非约束，避免 fail-closed 误伤正常查询）
    "经典", "典型", "常见", "最新", "高质量", "优质", "标准", "好", "好的", "优秀",
    "一份", "几个", "若干", "各种", "各类", "所有", "全部", "更多", "经典的",
    "帮忙", "看一下", "了解", "关注", "研究", "做", "用", "适合", "合适", "常用",
    # ── 出处/介词/动词虚词 ───────────────────────────────────
    # 起因：`推荐有 FASTQ 的人类乳腺癌数据，来自10x` 整句弃权，理由是「系统未收录的词：来自」。
    # 「来自」是**介词**，不是约束——把它当未收录实义词而整句弃权，是残差门把虚词误判成实词。
    # 本组全部是**功能词**（介词/连词/一般动词/量词/程度词），按定义不是任何维度的头或值，
    # 故进 GRAMMAR（不回显）——若进 DOMAIN 会回显「『来自』未作为筛选维度」，那是废话且像在推卸。
    # 三条筛子逐词过过：① 不是任何维度的值；② 朴素子串 replace 不切碎受控词表里的词
    #（词表 alias 在残差门之前就被消费掉了，故只需与**残差**互不吞并）；③ 不含否定语素
    #（含/带/无/非/不/免/除 一律不收，避免 _leftover_negation 先删 filler 再找否定时丢掉否定信号）。
    "来自", "出自", "源自", "源于", "来源于", "来源", "产自", "取自", "选自",
    "收录", "发布", "官方", "提供", "上传",
    "针对", "面向", "基于", "根据", "按照", "依据", "涉及", "通过", "筛选", "限定",
    "跑出来", "跑出", "生成", "产出", "得到", "做出", "测得", "处理", "处理过", "整理",
    "一批", "一套", "一系列", "大概", "大约", "差不多", "尽可能", "越多越好", "多一些",
    "化学", "试剂", "版本", "流程", "管线", "组学",
    # 英文对应虚词。注意残差门的英文分支按 `[a-z]{2,}` **逐词**取，且只有 len>=3 才算实义，
    # 所以多词短语（cell ranger）必须**拆成单词**分别登记，整条登记不会生效。
    "from", "via", "using", "used", "based", "generated", "produced", "made",
    "obtained", "derived", "hosted", "published", "available", "related",
    "source", "sources", "sequencing", "library", "libraries", "sample", "samples",
    "study", "studies", "atlas", "project", "projects", "platform", "technology",
    "assay", "experiment", "experiments", "cell", "cells", "any", "all", "more",
    "looking", "need", "list", "get", "top",
    # 英文里描述「数据被怎么整理过」的形容词。它们不是任何维度的值，却因为 ≥3 字母
    # 而被残差门当成未识别实义词 —— `integrated human lung atlas` 会整句弃权。
    # （这句话在收紧词边界之前更糟：`integrated` 里的 `rat` 让它悄悄多出一个物种约束。）
    "integrated", "curated", "annotated", "processed", "aggregated", "combined",
    "merged", "harmonized", "reference", "public", "collection", "resource", "database",
    # ── 已收「来自/出自/基于」，但**同一类**的还剩一批仍在整句弃权。
    # 实测：「2022 年之后发表」卡在「发表」、「要原始 FASTQ」卡在「要原始」——和「来自」一模一样，
    # 都是动词/虚词被当成未识别实义词。这次按词性成批收，不再一个一个补。
    "发表", "已发表", "出版", "汇总", "覆盖",
    "要", "原始", "联合", "结合", "配套", "同时", "另外", "此外", "其中", "包括", "包含",
    "published in", "reported", "deposited", "submitted", "released",
    # 分子/测序通用头词：本身不是任何可筛维度的值（scRNA / snRNA / ATAC 等**具体**写法都是受控
    # 别名、在残差门之前就被消费掉了），裸写时只是句子里的通用名词。
    # 实测「人类肝癌的单细胞 RNA 和 ATAC 联合数据」就卡在裸「rna」这三个字母上整句弃权。
    "rna", "dna", "mrna", "cdna", "seq", "omics",
    # ── 查询电池回归：仍在整句弃权的**纯功能词/已落维度的头词** ──────────
    # 判别标准仍是那三条筛子（不是任何维度的值 / 不切碎受控词表 / 不含否定语素），
    # 且必须是「报出来会撒谎」的那一类，才进 GRAMMAR 而不是 DOMAIN：
    #   · 「一下」：纯语气助词。实测「给我拉取一下人类肝脏的单细胞数据」整句弃权在「一下」上——
    #     「看一下」早就在表里，裸「一下」漏了，典型的按实例补而不是按词性补留下的洞。
    #   · 「感染」：实测「巨细胞病毒感染的单细胞数据」弃权在「感染」上，可 disease 已经落成
    #     Cytomegalovirus **Infection** ——它就是已落维度那个值的尾巴，回显「感染未作为筛选维度」
    #     是彻头彻尾的谎话。
    #     刻意**不**顺手收「综合征/病变」这类病名尾巴：它们没有实测背书，而且一旦成了 filler，
    #     「代谢综合征」这种整句都是 filler 的写法会从「诚实弃权」变成「把整个库倒出来 + 一行脚注」。
    #   · 「整合/整合分析」：与已在表里的「联合/结合」同类，是对数据怎么用的描述，不是维度值。
    #     实测「我需要一些多组学的人类数据集用来做整合分析」弃权在这里。
    #   · 「genomics」：厂商名 10x Genomics 的后半截（来源名走 auto_parse_sources 另一条路，
    #     不是 catalog alias），裸留在句子里就成了 7 个字母的「未识别实义词」。
    #     实测「10x Genomics 的人类外周血单个核细胞数据」整句弃权。与已在表里的 "omics" 同族。
    "一下", "感染", "整合", "整合分析", "genomics",
    # ── 2026-07-25 基线变更：「或」与 hedge 从「整句弃权」改为「照做」，两者的**残留**
    # 必须有落点，否则会从 unsupported_boolean_or / unsupported_hedge 掉进 unresolved_term
    # ——换了个弃权理由而已，用户照样什么都拿不到。
    #   · 「或 / 或者」：与已在表里的「和 / 与」同族的并列连词。同维度多值本身就是「或」的语义
    #     （见 OR_MARKERS 处的注释），连词本体不承载任何约束。
    #   · 「最好 / 尽量 / 如果可以 / 可以的话 / 如果有」：**先**由软偏好语法尝试消费（紧邻实体时
    #     变成加权），没消费掉的残留（如「最好的人类肺数据」里的「最好」）在这里作虚词落地。
    #     这是兜底、不是主路径；主路径见 HEDGE_PREFER_PREFIX_CN。
    #   · 「希望」：请求动词（同「想要 / 需要」），不进偏好表。
    "或", "或者", "最好", "尽量", "如果可以", "可以的话", "如果有", "希望", "如果",
    "preferably", "ideally", "possible",
    # ── 口语虚词，此前各自把整句卡进 unresolved_term 弃权 ──────────
    # 过同样三条筛子（不是任何维度的值 / 不切碎受控词表 / 不含否定语素）：
    #   · 「都行」（dv07/dv10）：「斑马鱼或者果蝇，哪个都行」的口语收尾，实体已落维，残留「哪都行」弃权。
    #   · 「就是」（dv07）：「…都行，就是别带脑组织的」的强调副词。
    #   · 「下到」（dv16）：「能下到原始数据吗」的口语下载动词；检索语义在「原始数据」（raw 别名）上，
    #     它本身只是问句谓语。刻意不进 ACTION_VERBS：本句是检索问句不是执行指令，路由层不该弹执行面板。
    #   · 「得」（dv18）：「得含原始测序数据」的「得」；单字 filler 把残差 run「得含」拆成单字「含」即放行。
    #     不收「含」——它紧贴否定前缀「不含/不带」，留在外面不参与 filler 替换更稳。
    #   · 「里头」（dv19）：「Visium 里头有 FASTQ 原始文件」的方位词。
    #   · 「能」（dv28）：「玉米的数据也能查到吗」的情态动词（同已在表里的「可以」一类）；
    #     「不能」的「不」是否定形素、先于/独立于本表被 guard 捕获，收「能」不吞否定信号。
    #   · 「检测」（dv55，holdout h28 句型）：「Xenium 原位检测的数据有没有」——「原位」被 Xenium 别名
    #     消费后残留「检测」二字触发 unresolved_term 整句弃权。「检测」是方法学通用尾词（同「测序/分析」一类），
    #     过三条筛子：① 不是任何维度的值（技术维落点是 xenium/atac 等专名，「检测」本身不落维）；
    #     ② 受控词表无任何 alias 含「检测」，且 alias 先于 filler 消费，不切碎词表；
    #     ③ 不含否定语素（不/没/无/非/未/勿/莫/别/否/免/除/含/带 均不沾）。
    "都行", "就是", "下到", "得", "里头", "能", "检测",
]
# 执行类动作词并入语法填充词——**用程序并，不手抄**。
# 两份手抄的清单必然漂移，这一轮当场就漂了一次：ACTION_MARKERS 里有「批量下载」，
# 手抄进 FILLER_GRAMMAR 时只抄了「下载」，于是「人类肺数据，批量下载」照样整句弃权。
# 本项目已在别处栽过同型（两份手抄清单从未对账），不再重复。
FILLER_GRAMMAR += [m for m in ACTION_MARKERS if m not in set(FILLER_GRAMMAR)]
# 结构上无筛选维度的实义描述词（性别/年龄/发育/受试者/功能类）——静默丢词诚实层据此回显 unused_query_terms。
# 仍作 filler 参与残差门（无对应可过滤维度 → 不因它们弃权，避免正常查询被误伤），只是**额外**被回显。
FILLER_DOMAIN = [
    "患者", "病人", "受试者", "供体", "捐赠者", "免疫",
    "成人", "成年", "儿童", "婴儿", "胎儿", "新生儿", "青少年", "老年", "年轻",
    "男性", "女性", "雄性", "雌性", "男", "女",
    # ── 实义描述词，系统结构上确实没有对应筛选维度 ──────────────
    # 这些词此前一律走 unresolved_term 整句弃权：用户写「转移性乳腺癌」连乳腺癌都查不到。
    # 它们既不是任何维度的头词、也没有可过滤的字段，正是 FILLER_DOMAIN 的定义域：
    # 不因它们弃权（否则整句白写），但**必须回显**「以下词未作为筛选维度」，否则就是静默丢词。
    # 受控词表 alias 在残差门**之前**消费，故「淋巴瘤 / 慢阻肺 / 急性髓系白血病」等长实体
    # 不会被这里的「淋巴 / 慢性 / 急性」切碎（长 alias 先落维、轮不到 filler）。
    # 细胞类型（没有 cell_type 维度）
    "神经元", "巨噬", "巨噬细胞", "上皮", "内皮", "成纤维", "间质", "基质",
    "髓系", "淋巴", "树突", "小胶质", "星形胶质", "浆细胞", "祖细胞",
    # 状态 / 分期 / 处理（没有 condition 维度）
    "对照", "对照组", "野生型", "野生", "敲除", "过表达", "治疗前", "治疗后", "治疗",
    "用药", "化疗", "放疗", "复发", "转移", "转移性", "原发", "分期",
    "早期", "中期", "晚期", "进展期", "急性", "慢性",
    # 规模 / 分辨率（样本量是排序 tie-breaker，不是可筛维度）
    "高深度", "深度", "万级", "千级", "十万", "大规模", "小规模", "高分辨率", "低分辨率",
    # 取材 / 保存（没有 preservation 维度）
    "新鲜", "冷冻", "冻存", "石蜡", "ffpe", "活检", "穿刺", "尸检", "手术", "术后",
    # 数据形态与工具（没有 file_format / software 维度；文件清单另有专门入口）
    "表达矩阵", "计数矩阵", "矩阵", "注释", "counts", "loom", "barcode", "matrix",
    "ranger", "cellranger", "seurat", "scanpy",
    # ── 验证里仍在整句弃权、但结构上确实没有对应筛选维度的研究主题词。
    # 「肿瘤微环境的单细胞数据」此前连「肿瘤」都查不到，就因为「微环境」不认识。
    # 它们不是任何维度的头词，故回显「未作为筛选维度」不会误报已落维度。
    "微环境", "肿瘤微环境", "免疫微环境", "浸润", "免疫浸润",
    "发育", "发育时序", "时序", "拟时序", "轨迹", "分化", "再生", "衰老", "稳态",
    "谱系", "亚型", "异质性", "多样性", "可塑性", "极化", "激活",
    "类器官", "器官", "原代", "细胞系", "共培养", "培养",
    # 细胞类型（仍无 cell_type 维度）——「毛细胞 / 单核细胞」这类还会**劫持别名**，
    # 见 ALIAS_PROTECTED_COMPOUNDS：先整体保护、再按描述词回显。
    "毛细胞", "单核细胞", "干细胞", "免疫细胞", "肿瘤细胞", "基质细胞", "间充质",
    "成体", "胚系",
    # ── 查询电池回归：病名**限定语**。系统只按病名落维，限定语没有对应字段。
    # 「特发性肺纤维化的单细胞转录组」实测整句弃权，而「肺纤维化单细胞数据」有 19 条——
    # 差别只在「特发性」三个字。它必须进 DOMAIN 而不是 GRAMMAR：disease 落的是 Pulmonary
    # Fibrosis（各种成因混在一起），把 idiopathic 这个限定悄悄丢掉、还不吭声，就是静默丢词；
    # 回显「『特发性』未作为筛选维度」既不撒谎，也让用户知道结果里混着非特发性的。
    "特发性", "原发性", "继发性", "获得性", "遗传性", "家族性",
]

# ---------- 别名保护复合词：整体屏蔽，防短别名把长词劫持 ----------
# 病根：中文别名是**裸子串**匹配（英文侧 已按词边界收紧，中文侧没有——中文本来不分词）。
# 于是「单核细胞」(monocyte) 里的「单核」被当成 modality=single-cell、「皮质醇」里的「皮质」被当成组织。
# 这和「非编码RNA 被拆成排除 编码RNA」是同一类事故，所以沿用同一套办法：在 alias 消费**之前**
# 把整词屏蔽掉。屏蔽后它不再落任何维度，按 FILLER_DOMAIN 语义回显「未作为筛选维度」，不静默丢弃。
#
# 只收**确有劫持**的词（每条都在测试 tests/test_alias_collision_guard.py 里钉死），不做预防性堆砌。
ALIAS_PROTECTED_COMPOUNDS = (
    "单核细胞",      # 含「单核」→ 会被当成单核测序（modality=single-cell）
    "胸腺嘧啶",      # 含「胸腺」→ 会被当成组织 Thymus
    "血管紧张素",    # 含「血管」「血」→ 会被当成组织 Blood Vessel / Blood
    "胰岛素",        # 含「胰」→ 会被当成组织 Pancreas
    # ── 验证：上面只收了「胰岛素」一个，**同一族**的其余成员全在裸奔。
    # 实测（全库 5665）每一条都挂上了看起来完全正常的组织标签：
    "肾上腺素",      # 含「肾上腺」→ 组织 Adrenal Gland
    "去甲肾上腺素",  # 同上（长词优先，必须单列，否则被「肾上腺素」切一半）
    "肝素",          # 含「肝」→ 组织 Liver
    "胸腺肽",        # 含「胸腺」→ 组织 Thymus
    "甲状腺素",      # 含「甲状腺」→ 组织 Thyroid
    # 「X炎」里语料**确实没有**对应疾病取值的：pharyngitis / laryngitis 全库 0 条，
    # 而裸「咽」「喉」有组织别名 → 「咽炎」变成 20 条咽弓/鼻咽组织数据，没有一条是咽炎。
    # 有数据的（肺炎/肾炎/肝炎/胃炎/心肌炎）走登记本体那条路，不进这张表。
    "咽炎", "喉炎",
)
# 刻意**不收**「皮质醇 / 糖皮质激素」这一族：办法是不登记裸「皮质」别名（只收「皮层 / 大脑皮层 /
# 大脑皮质」），从源头上就不产生劫持。能靠别名取舍避开的，就不要靠保护表兜——保护表越短越可信，
# tests/test_alias_collision_guard.py::test_protected_list_has_no_dead_entries 会把空转条目打红。
FILLER_TOKENS = FILLER_GRAMMAR + FILLER_DOMAIN

# ---------- 「或」组合：不再弃权，如实按引擎真实能力执行 ----------
# 2026-07-25 基线变更后重新审视：`OR_MARKERS` 曾让整句弃权（`unsupported_boolean_or`），
# 而**引擎本来就支持同维度的「或」**——`retriever.passes_hard_filter` 逐字写着「正向：须含任一 target」，
# 也就是 `constraints[dim] = [A, B]` 的语义就是 A 或 B。于是：
#   · 「人或小鼠的脑数据」→ species=[human, mouse] + tissue=[brain] —— **精确就是用户要的**；
#   · 「不要小鼠或大鼠」→ ¬(A∨B) = ¬A∧¬B，排除侧「命中任一 forbidden 即淘汰」也**精确**成立
#     （反而比「不要小鼠和大鼠」这种 ¬(A∧B) 更无歧义，而后者因为「和」是虚词一直照做）；
#   · 只有 OR 的两侧落在**不同维度**（「肺癌或 10x」）时表达不出来，按「同时满足」执行并**如实回显**
#     （`QueryIntent.or_handling`）。实测这种说法极少，且多数情况（「人类肺癌或小鼠肝癌」）
#     跨维度 AND + 维度内 OR 得到的是**超集**而不是更窄，宁可多给也不该整句作废。
# 保留本常量：仍用于**检测与回显**，只是不再触发弃权。
OR_MARKERS = ["或者", "或", " or "]

# ---------- hedge：从「一律弃权」改为「按软偏好照做」 ----------
# 旧注释（已作废）说「最好 / 尽量」改起来属于受控重基线、需要单独授权——产品方
# 已给出该授权，并明确纠正了「宁可弃权也不返回违背用户意图的结果」这条底线。
#
# 实测证据（同一天量的）：
#     「优先 Xenium 的黑色素瘤数据」→ 55 条
#     「最好是 Xenium 的黑色素瘤数据」→ 0 条、且**放宽选项 0、降级 0**，用户什么都拿不到
# 两句话除了「优先」/「最好是」逐字相同。更要紧的是冻结评测自己那条 adv07 就写着
# `nice_to_have: {"technology": "xenium"}` —— **评测数据本身就把「最好」建模成软偏好**，
# 弃权反而偏离了它。
#
# 「如果可以 / 如果有」也归到这里：它们的「条件不满足时怎么办」答案恰恰就是软偏好的定义
#（满足就排前面，不满足也照样给），并不是真的条件分支。
# 「希望」不进这张表：它在中文查询里基本是**请求动词**（「希望找人类肺数据」＝「想要」），
# 不是对某个具体值的倾向；已作为虚词进 FILLER_GRAMMAR。
HEDGE_PREFER_PREFIX_CN = ("最好", "尽量", "如果可以", "可以的话", "如果有",
                          "preferably", "ideally", "if possible")

# ---------- 软偏好语法：优先 X ----------
# 语义：X **不参与硬过滤**，只在排序里加权。这是和「只要 X」的根本区别——若把「优先」做成硬过滤，
# 用户说「优先 Visium」就再也看不到 Xenium 的数据，那是把偏好偷换成筛选、比不支持更糟。
# 执行边界仿照否定语法：只有「优先」**紧邻**一个受控词表实体（或 FASTQ 物理名、或来源专名）才执行；
# 后面跟着系统不认识的词时**不弃权**（软偏好丢了不会返回违反意图的结果，只是没排序加权），
# 而是把「优先」如实回显进「未作为筛选维度」，避免静默吞掉用户输入。
SOFT_PREFER_PREFIX_CN = ("优先考虑", "优先选择", "优先", "偏向", "倾向于", "倾向")

#: 标记词与实体之间允许夹一个虚字（「优先是 / 优先用 / 优先选 Visium」），再多就不猜了。
PREFER_CONNECTOR_CHARS = "是用选取要有带的"
#: hedge 类**刻意不含「的」**：「最好的人类肺数据」是「最好的」当形容词、人类和肺都是硬要求；
#: 允许「的」会把它读成「偏好人类」，把硬要求降级成加权 —— 那是反向的静默偏离。
#: 「最好是 / 尽量用 Xenium」这些真偏好写法都不靠「的」连接，所以去掉它零损失。
HEDGE_CONNECTOR_CHARS = "是用选取要有带"
#: 两族合成一张表供解析层遍历。**程序并，不手抄**（本仓库在 `ACTION_MARKERS↔FILLER_GRAMMAR`
#: 上栽过两次手抄漂移）。
PREFER_PREFIXES_ALL = tuple(SOFT_PREFER_PREFIX_CN) + tuple(HEDGE_PREFER_PREFIX_CN)

# 兼容旧引用：并集，供仍 import NEGATION_MARKERS 的调用方（现解析走下方结构化白名单/guard，不再靠此列表判定）
NEGATION_MARKERS = ["不要", "不需要", "无需", "别", "除了", "排除", "非", "不含", "不包含", "no ", "without", "exclude"]

# ============================================================================
# 否定 / 排除语法（v2：小白名单执行 + 大兜底弃权；经两轮对抗评审收敛）
# ----------------------------------------------------------------------------
# 原则：只有整条负向 clause 完全落在白名单里才提交 exclusion；任何未被白名单覆盖的否定
# 成分必然触发 guard 或残差门弃权 —— 结构性保证「绝不静默反向」，而非靠穷举自然语言。
# ============================================================================

# raw 三态只认物理资产名（不含「可复现/重跑」等间接说法 → 那些仍只作正向 RAW_REQUIRED）
RAW_TERMS = ("fastq", "原始数据", "raw data")

# 可执行否定前缀（中文）：紧跟同维实体列表才执行硬排除
EXEC_NEG_PREFIX_CN = (
    "不要", "没有", "没", "无",
    "不带", "不含", "不包含", "不包括",
    "排除", "剔除", "去除", "去掉", "排掉",
    "拒绝",
    # 「别带X」口语排除（「就是别带脑组织的」）。
    # 单字「别」仍留在 guard（只检测不执行）；「别带」是动宾复合、作用域明确，收为可执行前缀。
    "别带",
)
# 可执行否定前缀（英文，词边界 + 紧邻 typed target）
# （h41 英文否定改写盲区）：补 "not" 与 "free of"。环内 rerank 会把中文否定句
# （「淋巴瘤的不要」）改写成英文措辞重检，"not X"/"free of X" 此前只检测不执行 → 排除约束丢失。
# 执行口径与中文完全同款：词边界 + 紧邻受控词表实体列表才执行，其余一律落到 guard/残差门弃权。
# 「non」**刻意不收**：non-small cell lung cancer 的 non- 是词素不是排除操作符（同「非小细胞肺癌」
# 红线），它留在 guard 里只检测。「except/excepting」同样仍只检测（"except for" 等半截写法先弃权）。
EXEC_NEG_PREFIX_EN = ("no", "not", "without", "exclude", "excluding", "free of")
# 可执行环缀（开, 闭）：闭合词均为双字，避免 bare「外」在「外周血」首字误闭合
EXEC_NEG_CIRCUMFIX_CN = (("除了", "以外"), ("除了", "之外"), ("除", "以外"), ("除", "之外"))
# 可执行后缀（X 除外）
# 否定后缀两族——「X的不要」（淋巴瘤的不要）与
# 「X的就不用给了」（小鼠的就不用给了）。后缀以「的」起首：消费逻辑要求实体链**紧邻后缀左侧**结尾，
# 「的」作所有格黏在实体与否定谓语之间，必须连它一起圈进后缀，否则实体与「不要」之间永远隔着一个字。
EXEC_NEG_SUFFIX_CN = ("除外", "的不要", "的就不用给了")
# 实体列表连接词（仅这些安全）。用于否定子句与软偏好的「同段紧邻实体列表」消费。
# 加入「或 / 或者」：不加的话「不要小鼠或大鼠」只会排除掉小鼠，大鼠照样返回
# ——那是**静默的部分执行**，比整句弃权更糟（用户以为两个都排除了）。
# 语义上也正好：否定侧 ¬(A∨B) = ¬A∧¬B，正向侧同维度多值本来就是「或」，
# 两边都不需要额外规则，唯一缺的就是这个连接词本身。
LIST_CONNECTORS = ("和", "与", "及", "以及", "、", "或者", "或")

# 只检测、绝不执行的否定 guard：命中且未被完整白名单消费 → 弃权
NEGATION_GUARDS_CN = (
    "不是", "并非", "并不是", "不排除", "未排除", "不能排除",
    "不用", "不必", "无须", "无需", "不需要", "没必要", "没有必要",
    "不存在", "并无", "并没有", "不涉及",
    "禁止", "避免", "避开", "绕开",
    "删掉", "移除", "过滤掉", "拒收",
    # 单字否定成分兜底（未被上面多字词或实体 span 覆盖时弃权，不再依赖「残差≥2字」）
    "不", "没", "无", "非", "未", "勿", "莫", "别", "否", "免",
)
NEGATION_GUARDS_EN = (
    "not", "never", "except", "excepting", "other than",
    "avoid", "omit", "omitting", "skip", "reject", "remove",
    "free of", "lacking", "minus", "neither", "nor",
    "don't", "do not", "needn't", "need not", "not required", "non",
)

#: 否定语素全集（可执行否定前缀 ∪ 只检测 guard），长词优先。**程序并，不手抄**：
#: `board._NEG_MORPHEMES` 与执行层的极性门（`action_plan`）都消费这一份。
#: 本仓库在 `ACTION_MARKERS↔FILLER_GRAMMAR`、
#: `SOURCE_PREFER_PREFIX_RE↔SOFT_PREFER_PREFIX_CN` 上栽过手抄漂移，这里不再开口子。
NEG_MORPHEMES_CN = tuple(sorted(set(EXEC_NEG_PREFIX_CN) | set(NEGATION_GUARDS_CN), key=len, reverse=True))
NEG_MORPHEMES_EN = tuple(sorted(set(EXEC_NEG_PREFIX_EN) | set(NEGATION_GUARDS_EN), key=len, reverse=True))
# 否定豁免复合词：词首 非/non- 是词素、整体为正向生物学术语，非排除操作符。命中即在 _leftover_negation
# 扫描否定形素**之前**把该 span 屏蔽，防止「非编码RNA」被误当成「排除 编码RNA」→ unsupported_negation。
# 本表只收**无对应结构化维度可映射**者（能映射的走 CATALOG alias 正向消费：非人灵长类→species、
# 非小细胞肺癌/非霍奇金淋巴瘤→disease，均不入此表）。屏蔽后无维度可落 → _residual_salient 认作未收录实义词
# → unresolved_term 诚实弃权。全部小写（作用于 query.lower() 后的工作串），屏蔽时按长度降序先消费长词。
NEGATION_EXEMPT_COMPOUNDS = (
    "非编码rna", "长非编码rna", "非编码",
    "non-coding rna", "non coding rna", "non-coding", "non coding",
)
# 存在性问句：整体中性，不触发否定（「有没有小鼠脑数据」= 正向 mouse+brain）
NEG_EXISTENTIAL_PHRASES = ("有没有", "有无", "是否有", "是否包含", "是否含", "是否有的")
# 条件结构：整句弃权（除非/如果…不要…）
NEG_CONDITIONAL_MARKERS = ("如果", "假如", "要是", "除非", "若")
# 疑问结构：整句弃权（是否排除…）
NEG_INTERROGATIVE_MARKERS = ("是否排除", "要不要排除", "是否要排除", "该不该排除")

import re as _re

# raw 硬排除模式 → has_raw_data_required=False（作用于 lowercase 查询）
RAW_FORBIDDEN_PATTERNS = tuple(_re.compile(p) for p in (
    r"(?:不要|没有|没|无|不带|不含|不包含|不包括|排除|剔除|去除|去掉|排掉|拒绝)\s*(?:fastq|原始数据)",
    r"(?:fastq|原始数据)\s*除外",
    r"(?:除了|除)\s*(?:fastq|原始数据)\s*(?:以外|之外)",
    r"(?<![a-z0-9_])no(?:\s+|-)(?:fastq|raw(?:\s+|-)data)(?![a-z0-9_])",
    # not/free of 随 EXEC_NEG_PREFIX_EN 转正后必须在 raw 专用层同步收编。
    # raw span 不是结构化 dim；若落到 query_parser 4d 的通用 _apply_exclude，它会被完整
    # excise 却不会写 staged_raw，形成「看似执行、实际不筛 FASTQ」的静默反向。故这里
    # 先按 raw 真语义设置 has_raw_data_required=False；非 raw 对象仍走 4d typed exclusion。
    r"(?<![a-z0-9_])(?:without|exclude|excluding|not|free\s+of)\s+"
    r"(?:fastq|raw(?:\s+|-)data)(?![a-z0-9_])",
))
# raw 第三态：clarify（歧义需澄清）/ drop_constraint（明确不筛 raw）
RAW_OPTIONAL_PATTERNS = tuple((_re.compile(p), action) for p, action in (
    (r"(?:不需要|无需|无须|不用|不必|没必要)\s*(?:fastq|原始数据)", "clarify"),
    (r"(?:fastq|原始数据)\s*(?:不需要|不是必需|非必需)", "clarify"),
    (r"(?<![a-z0-9_])(?:don't need|do not need|need not)\s+(?:fastq|raw data)", "clarify"),
    (r"(?:fastq|raw data)\s+(?:not required|not necessary)", "clarify"),
    (r"(?:fastq|原始数据)\s*可有可无", "drop_constraint"),
    (r"(?:对\s*)?(?:fastq|原始数据)\s*(?:没有要求|不作要求)", "drop_constraint"),
    (r"with or without\s+(?:fastq|raw data)", "drop_constraint"),
    (r"(?:fastq|raw data)\s+optional", "drop_constraint"),
    (r"no requirement for\s+(?:fastq|raw data)", "drop_constraint"),
))


# ---------- 规则可识别规范词提示（供 LLM 重排审核改写用；只读、纯确定性）----------
# 背景：rerank_audit 让 LLM 对照原句审核规则抽词是否完整，不完整则**改写**成规则更易正确解析的句式。
# 改写要落到规则真正认识的词面上，故把 CATALOG 各维度的 display 规范名按维度列给 LLM 作候选。
# 只列 display（规范名，紧凑）、不列全部 alias（太长）；absent 物种也列（让规则"看见"约束→诚实无结果）。
# 纯读 CATALOG、无副作用；供 workflow 构造审核提示词时调用，rerank.py 不直接 import 本模块（保持分层解耦）。
_DIM_LABELS_CN: dict[str, str] = {
    "species": "物种", "tissue": "组织", "disease": "疾病",
    "platform": "平台", "assay": "实验技术", "modality": "数据模态",
}


def known_terms_hint(max_per_dim: int = 60) -> str:
    """按维度列出 CATALOG 的规范 display 名，作为 LLM 改写查询时的『规则认识的词』候选。

    返回形如：
        物种：Human、Mouse、Rat、Macaque、…
        组织：Breast、Lung、Brain、…
    确定性、可复现；维度顺序与标签取自 _DIM_LABELS_CN，未登记维度按 CATALOG 原键名兜底。
    """
    lines: list[str] = []
    for dim, entries in CATALOG.items():
        if not isinstance(entries, list):
            continue
        seen: list[str] = []
        for e in entries:
            disp = str((e or {}).get("display") or "").strip()
            if disp and disp not in seen:
                seen.append(disp)
            if len(seen) >= max_per_dim:
                break
        if seen:
            label = _DIM_LABELS_CN.get(dim, dim)
            lines.append(f"{label}：{'、'.join(seen)}")
    return "\n".join(lines)


def _is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def suggestable_terms(dim: str, max_n: int = 8) -> list[dict]:
    """给用户当「点一下就能用」的候选说法。只读、确定性。

    与 `known_terms_hint` 的关键差别：**只收 display 本身也是自己 alias 的条目**。
    像 Eye（别名只有 眼睛/眼部）、Rice（只有 水稻/oryza）、Cancer 这些条目，display 并不是任何 alias，
    照抄给用户会诱导他输入一个必然触发未收录实义词、整句弃权的词——那是把系统的内部规范名当成用户词。

    每项返回 `{alias, display}`：`alias` 是**真的能打进去用**的写法（优先该条目里最长的中文别名，
    没有中文别名时退回 display），`display` 是列表上展示的规范名。
    调用方必须如实说明这是「词表里认识的说法」，不是「库里存在的取值」——能不能搜到要搜了才知道。
    """
    out: list[dict] = []
    for entry in CATALOG.get(str(dim or "").strip(), []) or []:
        if not isinstance(entry, dict):
            continue
        display = str(entry.get("display") or "").strip()
        if not display:
            continue
        aliases = [str(a).lower().strip() for a in entry.get("aliases", []) if str(a).strip()]
        if display.lower() not in aliases:
            continue
        cjk = sorted((a for a in aliases if _is_cjk(a)), key=len, reverse=True)
        out.append({"alias": cjk[0] if cjk else display, "display": display})
        if len(out) >= max(1, int(max_n)):
            break
    return out
