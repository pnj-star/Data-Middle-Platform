-- MySQL schema for document pipeline storage (replaces SQLite).
-- Run once before deploying the new code.

CREATE TABLE IF NOT EXISTS `metadata` (
    `id`                  VARCHAR(32)  NOT NULL COMMENT '文档 UUID',
    `name`                VARCHAR(512) NOT NULL COMMENT '文件名',
    `stored_path`         VARCHAR(1024) NULL DEFAULT NULL COMMENT '原始文件路径',
    `size`                BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '文件大小(bytes)',
    `type`                VARCHAR(16)  NOT NULL DEFAULT 'text' COMMENT '类型: text / image',
    `extension`           VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '扩展名',
    `mushroom_type`       VARCHAR(128) NOT NULL DEFAULT '' COMMENT '业务分类',
    `product_id`          VARCHAR(128) NOT NULL DEFAULT '' COMMENT '产品 ID',
    `tenant_id`           VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '租户 ID',
    `kb_id`               VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '知识库 ID',
    `status`              VARCHAR(32)  NOT NULL DEFAULT 'uploaded' COMMENT '管道状态',
    `error`               TEXT         NULL DEFAULT NULL COMMENT '最近一次错误信息',
    `output_path`         VARCHAR(1024) NULL DEFAULT NULL COMMENT '处理后产物路径',
    `chunk_count`         INT UNSIGNED NULL DEFAULT NULL COMMENT '子块数量缓存',
    `previous_tenant_id`  VARCHAR(64)  NULL DEFAULT NULL COMMENT '元数据修改前的旧租户 ID',
    `previous_kb_id`      VARCHAR(64)  NULL DEFAULT NULL COMMENT '元数据修改前的旧知识库 ID',
    `created_at`          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at`          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    KEY `idx_tenant_kb` (`tenant_id`, `kb_id`),
    KEY `idx_status` (`status`),
    KEY `idx_name` (`name`(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='文档元数据表（原 SQLite files）';


CREATE TABLE IF NOT EXISTS `markdown` (
    `file_id`          VARCHAR(32) NOT NULL COMMENT '关联 metadata.id',
    `markdown`         MEDIUMTEXT  NULL COMMENT '清洗后的 Markdown 全文（切块/入库权威内容）',
    `raw_markdown`     MEDIUMTEXT  NULL COMMENT '原始转换结果，保留不覆盖',
    `metadata_json`    JSON        NULL COMMENT 'MinerU 转换元信息',
    `status`           VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending / done / failed',
    `clean_state`      VARCHAR(16) NOT NULL DEFAULT 'raw' COMMENT 'raw / cleaned / edited',
    `clean_report_json` JSON       NULL COMMENT '清洗动作报告',
    `cleaner_version`  VARCHAR(32) NULL COMMENT '清洗器版本',
    `cleaned_at`       DATETIME(3) NULL COMMENT '最近一次成功清洗时间',
    `markdown_version` INT UNSIGNED NOT NULL DEFAULT 1 COMMENT '每次编辑 +1',
    `created_at`       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at`       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`file_id`),
    CONSTRAINT `fk_markdown_file` FOREIGN KEY (`file_id`)
        REFERENCES `metadata`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Markdown 转换结果表（原 SQLite conversions）';


CREATE TABLE IF NOT EXISTS `chunks` (
    `id`             VARCHAR(32)  NOT NULL COMMENT '子块 UUID',
    `seq`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE COMMENT 'chunk insertion order (stable document order)',
    `file_id`        VARCHAR(32)  NOT NULL COMMENT '关联 metadata.id',
    `parent_id`      VARCHAR(128) NULL DEFAULT NULL COMMENT '父块业务 ID（rag_parent_block.parent_id）',
    `parent_title`   VARCHAR(512) NULL DEFAULT NULL COMMENT '父块标题快照',
    `child_content`  TEXT         NOT NULL COMMENT '子块正文',
    `chunk_index`    INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '父块内序号',
    `child_size`     INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '子块 token 数',
    `parent_size`    INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '父块 token 数',
    `parent_hash`    CHAR(64)     NULL DEFAULT NULL COMMENT '父块归一化哈希',
    `child_hash`     CHAR(64)     NULL DEFAULT NULL COMMENT '子块归一化哈希',
    `deduplicated`   TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '是否因去重跳过嵌入',
    `created_at`     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    KEY `idx_chunks_file` (`file_id`),
    KEY `idx_chunks_parent` (`parent_id`),
    CONSTRAINT `fk_chunks_file` FOREIGN KEY (`file_id`)
        REFERENCES `metadata`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='子块表（原 SQLite chunks）';


CREATE TABLE IF NOT EXISTS `publish_staging` (
    `file_id`          VARCHAR(32) NOT NULL COMMENT '关联 metadata.id',
    `parent_rows_json` MEDIUMTEXT  NOT NULL COMMENT '暂存的父块元数据 JSON',
    `created_at`       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`file_id`),
    CONSTRAINT `fk_staging_file` FOREIGN KEY (`file_id`)
        REFERENCES `metadata`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='发布暂存表（原 SQLite publish_staging）';


CREATE TABLE IF NOT EXISTS `ingestion_jobs` (
    `job_id`           VARCHAR(32)  NOT NULL COMMENT '任务 UUID',
    `file_id`          VARCHAR(32)  NOT NULL COMMENT '关联 metadata.id',
    `action`           VARCHAR(32)  NOT NULL DEFAULT 'publish' COMMENT 'publish / delete / reindex',
    `state`            VARCHAR(32)  NOT NULL DEFAULT 'pending' COMMENT '状态机当前状态',
    `target_version`   INT UNSIGNED NULL DEFAULT NULL,
    `expected_chunks`  INT UNSIGNED NULL DEFAULT NULL,
    `indexed_chunks`   INT UNSIGNED NULL DEFAULT NULL,
    `retry_count`      INT UNSIGNED NOT NULL DEFAULT 0,
    `last_error`       TEXT         NULL DEFAULT NULL,
    `created_at`       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at`       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`job_id`),
    KEY `idx_jobs_file` (`file_id`),
    KEY `idx_jobs_state` (`state`),
    CONSTRAINT `fk_jobs_file` FOREIGN KEY (`file_id`)
        REFERENCES `metadata`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='发布任务状态机（原 SQLite ingestion_jobs）';
