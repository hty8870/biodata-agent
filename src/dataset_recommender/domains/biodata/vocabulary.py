"""biodata 领域词表（catalog）+ 平台/assay/modality 归一规则。

2026-09-23 骨肉分离批自 `retrieval/vocabulary.py` 迁入本领域包：查询词法（语言级）
留在骨架 `retrieval/query_lexicon.py`；catalog 驱动的通用机制（suggestable_terms /
拼音索引）在 `domain/vocab_tools.py`。本模块只含 biodata 的领域数据与派生规则。

设计要点（与 Codex 辩论收敛结论一致）：
- hard_filter 的可靠性 = 约束抽取的召回率。词表尽量覆盖语料真实词汇 + 常见别名，
  并**收录库中不存在的已知实体**（如 elephant/大象），好让解析器"看见"约束→正确返回无结果。
- platform_family（Visium/Xenium/Chromium/Atera）与 assay（ATAC/Multiome/Flex/CNV/GEX…）
  是两个不同维度，分开建模，不再混用凌乱的 chemistry 串。
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
# 背景（反馈 + 3 视角对抗评审收敛）：`单细胞` 原硬映射成 platform=chromium，多源检索会静默排除
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
# alias 覆盖批次1（tissue+species）：并入 Uberon(组织)/NCBI Taxonomy(物种) 常见同义词 + 中文口语/临床变体（如
#   homo sapiens/mus musculus/表皮/乳房/骨骼肌…）。只扩 aliases 提召回，**不动 targets(规范键)**；匹配是子串+长优先消费，
#   故新增同义词只会把原本会 fail-closed 弃权的说法正确解析，绝不改变 hard_filter 的 0% 违规保证。
# alias 覆盖批次2（外部平台库对齐）：基础库(10x)为 base、外部平台库(CELLxGENE/HCA/EBI SCEA/ArrayExpress/ENCODE，约4900条)
#   含大量 base 没有的 species/tissue/disease/技术（拟南芥/线虫/酵母/胸腺/小脑/视网膜/新冠/阿尔茨海默/帕金森/糖尿病/
#   Smart-seq2/Drop-seq/Slide-seq/MERFISH…）。这些说法此前一律 fail-closed 弃权 → 漏掉数百条真实外部记录。本批**新增受控项**
#   （非仅扩别名）：每条仍是「alias→子串 target」结构，0% 违规由 hard_filter 结构性保证不变；新 target 只让对应约束能被识别+硬过滤。
#   技术类（Smart-seq2 等）归入 assay 维度（匹配 assay+chemistry 子串），不走 platform 的精确 family 匹配，避免改核心归一。
#   纪律：每批扩充后跑 scripts/evaluate_recommendation.py（54 题冻结门，base-only），确认 Constraint_Violation 仍=0%、Top5/NoResult 不退化，再合。
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
        # 玉米（2026-08-06 dev 集 dv28/dv29/dv33）：base 有 2 条 species="Maize" 记录，
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
        # ── 2026-07-22 夜 批次3：语料实证覆盖扩充（组织）──────────────────────────────
        # 挑选依据不是拍脑袋，是**查询侧覆盖率实测**：拿 106 个常见中文检索词逐个跑 parse_query，
        # 挑出「语料里确实有数据、却因为词表没这个词而整句 unresolved_term 弃权」的那些
        # （皮层 332 条 / 口腔 97 / 纹状体 72 / 膀胱 35 / 肾上腺 28 / 下丘脑 25 …共 66 个）。
        # 每个 target 子串都回语料核对过它到底命中哪些取值，专门防「过宽」：
        #   · 裸 "oral" 会命中 middle temp**oral** gyrus（39 条颞叶数据混进口腔）→ 改用 oral cavity/buccal/mouth；
        #   · 裸 "caudate" 会命中 caudate lobe of liver（29 条肝尾状叶混进纹状体）→ 改用 caudate nucleus/-putamen；
        #   · 裸 "bone" 里 189/225 是 bone marrow（已有独立概念）→ 本批**不收**「骨」，宁可继续弃权。
        # 同理**不收**「白质」：它是「蛋**白质**」的真子串，会把蛋白质组学查询劫持成组织=白质
        # （正是本轮要消灭的那类事故），只收无歧义的「脑白质」形不值当，留待 F2 保护机制后再议。
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
        # 本批拆开：组织归这里，癌症在 disease 侧改名为「子宫内膜癌」。
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
        {"aliases": ["肺癌", "lung cancer", "nsclc", "non-small cell lung", "非小细胞肺癌", "luad", "肺腺癌"],
         "targets": ["lung cancer", "non-small cell lung"], "display": "Lung Cancer"},
        {"aliases": ["肝癌", "liver cancer", "hcc", "hepatocellular"],
         "targets": ["hepatocellular", "liver cancer"], "display": "Liver Cancer"},
        # 「非霍奇金」词首「非」是词素、非排除（此前误报 unsupported_negation）。
        # 不收 3 字裸缩写 "nhl"：会子串误命中域内知名机构名 NHLBI（→静默注入 disease=lymphoma），全称/中文形已足够。
        # 2026-08-06 （dev 集 dv34）：霍奇金亚型拆出独立条目（见下）——此前「霍奇金淋巴瘤」泛化到 lymphoma，
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
        {"aliases": ["胶质母细胞瘤", "胶质瘤", "glioblastoma", "glioma", "gbm", "交质母细胞瘤", "交质瘤"], "targets": ["glioblastoma", "glioma"], "display": "Glioblastoma"},
        {"aliases": ["胃癌", "gastric cancer", "gastric adenocarcinoma"], "targets": ["gastric"], "display": "Gastric Cancer"},
        # 2026-08-06 （dev 集 dv37/dv38）：aml / acute myeloid 从泛称条目移入下方急髓专条——
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
        {"aliases": ["溃疡性结肠炎", "ulcerative colitis", "溃结", "溃样性结肠炎"], "targets": ["ulcerative colitis"], "display": "Ulcerative Colitis"},
        {"aliases": ["炎症性肠病", "inflammatory bowel disease", "ibd"], "targets": ["inflammatory bowel"], "display": "IBD"},
        {"aliases": ["慢阻肺", "慢性阻塞性肺", "chronic obstructive pulmonary", "copd"], "targets": ["chronic obstructive"], "display": "COPD"},
        {"aliases": ["哮喘", "asthma", "asthmatic"], "targets": ["asthma"], "display": "Asthma"},
        {"aliases": ["肺纤维化", "肺间质纤维化", "间质性肺", "pulmonary fibrosis", "interstitial lung"], "targets": ["pulmonary fibrosis", "interstitial lung"], "display": "Pulmonary Fibrosis"},
        {"aliases": ["囊性纤维化", "cystic fibrosis"], "targets": ["cystic fibrosis"], "display": "Cystic Fibrosis"},
        {"aliases": ["银屑病", "牛皮癣", "psoriasis", "psoriatic"], "targets": ["psoriasis", "psoriatic"], "display": "Psoriasis"},
        {"aliases": ["神经母细胞瘤", "neuroblastoma"], "targets": ["neuroblastoma"], "display": "Neuroblastoma"},
        # ── 2026-07-22 夜 批次3：语料实证覆盖扩充（疾病）──────────────────────────────
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
        {"aliases": ["肾细胞癌", "肾癌", "renal cell carcinoma", "kidney cancer", "rcc"],
         "targets": ["renal cell", "kidney cancer"], "display": "Renal Cell Carcinoma"},
        {"aliases": ["膀胱癌", "尿路上皮癌", "bladder cancer", "urothelial carcinoma"],
         "targets": ["bladder cancer", "urothelial"], "display": "Bladder Cancer"},
        {"aliases": ["甲状腺癌", "thyroid cancer", "thyroid carcinoma"],
         "targets": ["thyroid cancer", "thyroid carcinoma"], "display": "Thyroid Cancer"},
        {"aliases": ["多发性骨髓瘤", "骨髓瘤", "multiple myeloma", "myeloma"], "targets": ["myeloma"], "display": "Myeloma"},
        {"aliases": ["骨肉瘤", "肉瘤", "osteosarcoma", "sarcoma"], "targets": ["sarcoma"], "display": "Sarcoma"},
        {"aliases": ["髓母细胞瘤", "medulloblastoma", "髓母"], "targets": ["medulloblastoma"], "display": "Medulloblastoma"},
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
        {"aliases": ["慢性肾脏病", "慢性肾病", "chronic kidney disease", "慢姓肾病", "慢肾"], "targets": ["chronic kidney disease"], "display": "Chronic Kidney Disease"},
        {"aliases": ["肾小球肾炎", "肾病", "肾炎", "nephropathy", "nephritis", "glomerulonephritis"],
         "targets": ["nephropathy", "nephritis"], "display": "Nephropathy/Nephritis"},
        {"aliases": ["肺炎", "pneumonia"], "targets": ["pneumonia"], "display": "Pneumonia"},
        # 2026-07-22 夜对抗评审：「血管瘤」此前命中裸「血管」→ 组织=Blood Vessel/Artery/Vein
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
                     "scrnaseq", "snrnaseq",
                     # 2026-09-11 OOD 批次：错字/拼音形态（真实语域查询实测高频缺口）
                     "单细包", "单系胞", "danxibao"],
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
        # 2026-08-06 （dev 集 dv23-dv27）：① 补「V(D)J」带括号形、「免疫受体（库）」、「TCR/BCR」别名——
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
        # 2026-07-22 批次：此前一律 unresolved_term 弃权的单细胞/空间技术专名。
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
        # 2026-08-06 批次（HuBMAP 接入）：HuBMAP chemistry 高频的空间/多重成像技术专名。
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
        # ── 2026-08-06 批次（SCP/GEO NL 检索补盲）：两源 description 里实测高频、此前一律
        # unresolved_term 弃权的单细胞方法学专名（词边界计数：perturb-seq 14 / snuc-seq 9 /
        # dronc-seq 4 / pick-seq 4 / div-seq 2 条记录）。全部 ≥6 字符带连字符（无连字符变体
        # ≥7 字符），ASCII 词边界保护下不与普通英文词碰撞；只收连字/全称形，不收裸词根
        # （div/pick/snuc 裸形太泛，会误命中日常英语）。target 即方法名子串：T37 反标富化会把
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
