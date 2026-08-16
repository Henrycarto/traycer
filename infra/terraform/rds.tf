# Relational store, time series store and cache.
#
# Two Postgres instances rather than one. The relational store holds patient
# profiles, clinician panels and alert history: small, transactional, and kept
# under medical record retention rules. The Timescale instance
# holds the behavioral sample stream: large, append heavy, compressed after
# thirty days and dropped after two years. Sharing one instance would force the
# stricter retention and the heavier backup schedule onto both.

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.data[*].id

  tags = { Name = "${local.name}-db" }
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-postgres"
  family = "postgres16"

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  # Connections are logged so an access review can reconstruct who reached the
  # database and when.
  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }
}

resource "random_password" "relational" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "relational" {
  name       = "${local.name}/rds/relational"
  kms_key_id = aws_kms_key.traycer.arn
}

resource "aws_secretsmanager_secret_version" "relational" {
  secret_id = aws_secretsmanager_secret.relational.id
  secret_string = jsonencode({
    username = "traycer"
    password = random_password.relational.result
    host     = aws_db_instance.relational.address
    port     = 5432
    dbname   = "traycer"
  })
}

resource "aws_db_instance" "relational" {
  identifier     = "${local.name}-relational"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.environment == "production" ? "db.t4g.medium" : "db.t4g.small"

  allocated_storage     = 50
  max_allocated_storage = 500
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.traycer.arn

  db_name  = "traycer"
  username = "traycer"
  password = random_password.relational.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.data.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name
  publicly_accessible    = false

  multi_az = var.environment == "production"

  # Medical record retention rules outlive any product decision, so the
  # relational store keeps the maximum automated window and ships snapshots
  # onward from there.
  backup_retention_period   = 35
  backup_window             = "02:00-03:00"
  maintenance_window        = "sun:03:30-sun:04:30"
  copy_tags_to_snapshot     = true
  deletion_protection       = var.environment == "production"
  skip_final_snapshot       = var.environment != "production"
  final_snapshot_identifier = var.environment == "production" ? "${local.name}-relational-final" : null

  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.traycer.arn
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  auto_minor_version_upgrade = true

  tags = { Name = "${local.name}-relational", Store = "relational" }
}

resource "random_password" "timeseries" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "timeseries" {
  name       = "${local.name}/rds/timeseries"
  kms_key_id = aws_kms_key.traycer.arn
}

resource "aws_secretsmanager_secret_version" "timeseries" {
  secret_id = aws_secretsmanager_secret.timeseries.id
  secret_string = jsonencode({
    username = "traycer"
    password = random_password.timeseries.result
    host     = aws_db_instance.timeseries.address
    port     = 5432
    dbname   = "traycer_metrics"
  })
}

# TimescaleDB runs as a Postgres extension. On RDS that means the community
# edition, which carries hypertables, continuous aggregates and compression.
# The features this platform depends on are all in that set.
resource "aws_db_instance" "timeseries" {
  identifier     = "${local.name}-timeseries"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.environment == "production" ? "db.m6g.large" : "db.t4g.medium"

  # Sized for the sample volume rather than the row count: forty patients at
  # hourly windows across fifteen features is roughly three million rows a
  # year before compression, and the headroom is for a larger panel.
  allocated_storage     = 200
  max_allocated_storage = 2000
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.traycer.arn

  db_name  = "traycer_metrics"
  username = "traycer"
  password = random_password.timeseries.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.data.id]
  parameter_group_name   = aws_db_parameter_group.timescale.name
  publicly_accessible    = false

  multi_az = var.environment == "production"

  # Shorter than the relational store. A lost day of behavioral samples costs
  # a gap in a trajectory; a lost day of alert history costs an audit finding.
  backup_retention_period = 14
  backup_window           = "02:00-03:00"
  maintenance_window      = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot   = true
  deletion_protection     = var.environment == "production"
  skip_final_snapshot     = var.environment != "production"

  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.traycer.arn
  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = { Name = "${local.name}-timeseries", Store = "timeseries" }
}

resource "aws_db_parameter_group" "timescale" {
  name   = "${local.name}-timescale"
  family = "postgres16"

  parameter {
    name         = "shared_preload_libraries"
    value        = "timescaledb,pg_stat_statements"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "timescaledb.telemetry_level"
    value = "off"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "2000"
  }
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-cache"
  subnet_ids = aws_subnet.data[*].id
}

# Redis carries the alert queue and the notification dedup state. Both are
# recoverable from Postgres, which is why this runs without a replica outside
# production.
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${local.name}-cache"
  description          = "Alert queue and notification state"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = var.environment == "production" ? "cache.t4g.small" : "cache.t4g.micro"
  port           = 6379

  num_cache_clusters         = var.environment == "production" ? 2 : 1
  automatic_failover_enabled = var.environment == "production"

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.data.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  kms_key_id                 = aws_kms_key.traycer.arn

  snapshot_retention_limit = 3
  maintenance_window       = "sun:05:30-sun:06:30"

  tags = { Name = "${local.name}-cache" }
}

resource "random_password" "jwt_signing" {
  length  = 64
  special = false
}

# Internal service token signing key. Clinician tokens are asymmetric and are
# verified against the EHR JWKS, so this key only ever signs machine to
# machine calls.
resource "aws_secretsmanager_secret" "jwt_signing" {
  name       = "${local.name}/jwt/signing-key"
  kms_key_id = aws_kms_key.traycer.arn
}

resource "aws_secretsmanager_secret_version" "jwt_signing" {
  secret_id     = aws_secretsmanager_secret.jwt_signing.id
  secret_string = random_password.jwt_signing.result
}

# Firebase service account for clinician push. Written by hand once and read
# by the pulse-api task at start.
resource "aws_secretsmanager_secret" "fcm" {
  name       = "${local.name}/fcm/service-account"
  kms_key_id = aws_kms_key.traycer.arn
}

output "relational_endpoint" {
  value     = aws_db_instance.relational.address
  sensitive = true
}

output "timeseries_endpoint" {
  value     = aws_db_instance.timeseries.address
  sensitive = true
}

output "cache_endpoint" {
  value     = aws_elasticache_replication_group.main.primary_endpoint_address
  sensitive = true
}
