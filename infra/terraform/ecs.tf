# ECS Fargate services behind one application load balancer.
#
# Fargate rather than EC2 because none of these workloads need a GPU, a
# persistent disk or a custom kernel, and the operational cost of patching a
# node group is real money for a small team. The voice service is the one
# workload with a heavy container, and it scales to zero between samples.

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "services" {
  for_each = toset(local.services)

  name              = "/traycer/${var.environment}/${each.key}"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.traycer.arn
}

locals {
  services = ["voice", "scoring", "pulse-api", "web"]

  service_ports = {
    voice       = 8000
    scoring     = 8000
    "pulse-api" = 8000
    web         = 3000
  }

  # Sized from what each service actually does. Scoring holds thirty day
  # numpy slices per patient, so it gets the memory. Voice runs a subprocess
  # with its own address space limit and needs CPU during extraction. The
  # dashboard renders server components and is the cheapest of the four.
  service_cpu = {
    voice       = 1024
    scoring     = 1024
    "pulse-api" = 512
    web         = 512
  }

  service_memory = {
    voice       = 2048
    scoring     = 4096
    "pulse-api" = 1024
    web         = 1024
  }

  service_desired_count = {
    voice       = var.environment == "production" ? 2 : 1
    scoring     = var.environment == "production" ? 2 : 1
    "pulse-api" = var.environment == "production" ? 2 : 1
    web         = var.environment == "production" ? 2 : 1
  }
}

resource "aws_ecr_repository" "services" {
  for_each = toset(local.services)

  name                 = "traycer-${each.key}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.traycer.arn
  }
}

resource "aws_ecr_lifecycle_policy" "services" {
  for_each   = aws_ecr_repository.services
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The execution role pulls secrets at task start. It can read the specific
# secrets this platform owns and nothing else in the account.
resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-traycer-secrets"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.relational.arn,
          aws_secretsmanager_secret.timeseries.arn,
          aws_secretsmanager_secret.jwt_signing.arn,
          aws_secretsmanager_secret.fcm.arn,
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [aws_kms_key.traycer.arn]
      }
    ]
  })
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
}

resource "aws_iam_role_policy" "task" {
  name = "traycer-task"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${aws_s3_bucket.ingest.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [aws_kms_key.traycer.arn]
      }
    ]
  })
}

resource "aws_ecs_task_definition" "services" {
  for_each = toset(local.services)

  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.service_cpu[each.key]
  memory                   = local.service_memory[each.key]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = each.key
      image     = "${aws_ecr_repository.services[each.key].repository_url}:latest"
      essential = true

      portMappings = [{
        containerPort = local.service_ports[each.key]
        protocol      = "tcp"
      }]

      environment = [
        { name = "TRAYCER_ENV", value = var.environment },
        { name = "LOG_LEVEL", value = "INFO" },
        { name = "PULSE_API_INTERNAL_URL", value = "http://pulse-api.traycer.local:8000" },
        { name = "SCORING_SERVICE_URL", value = "http://scoring.traycer.local:8000" },
        { name = "VOICE_API_INTERNAL_URL", value = "http://voice.traycer.local:8000" },
      ]

      secrets = [
        { name = "JWT_SIGNING_KEY", valueFrom = aws_secretsmanager_secret.jwt_signing.arn },
        { name = "POSTGRES_DSN", valueFrom = "${aws_secretsmanager_secret.relational.arn}:dsn::" },
        { name = "TIMESCALE_DSN", valueFrom = "${aws_secretsmanager_secret.timeseries.arn}:dsn::" },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.services[each.key].name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
        }
      }

      healthCheck = {
        command  = ["CMD-SHELL", "curl -fsS http://localhost:${local.service_ports[each.key]}/health || exit 1"]
        interval = 30
        timeout  = 5
        retries  = 3
        startPeriod = 30
      }

      # Containers run read only with a writable scratch mount. The voice
      # service writes one temporary audio file per request and deletes it
      # before responding, so tmpfs is the right place for it: nothing
      # survives the task, and nothing lands on a persistent volume.
      readonlyRootFilesystem = true
      mountPoints = [{
        sourceVolume  = "scratch"
        containerPath = "/tmp"
        readOnly      = false
      }]
    }
  ])

  volume {
    name = "scratch"
  }
}

resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "traycer.local"
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "services" {
  for_each = toset(local.services)

  name = each.key

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "aws_ecs_service" "services" {
  for_each = toset(local.services)

  name            = "traycer-${each.key}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.services[each.key].arn
  desired_count   = local.service_desired_count[each.key]
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.services[each.key].arn
  }

  # Only the dashboard and the pulse API are reachable from outside. Scoring
  # and voice are internal and are addressed through service discovery.
  dynamic "load_balancer" {
    for_each = contains(["web", "pulse-api"], each.key) ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.public[each.key].arn
      container_name   = each.key
      container_port   = local.service_ports[each.key]
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  enable_execute_command = var.environment != "production"

  lifecycle {
    # The image tag moves on every deploy, and the deploy pipeline owns it.
    ignore_changes = [task_definition]
  }
}

resource "aws_lb" "public" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  drop_invalid_header_fields = true
  enable_deletion_protection = var.environment == "production"

  tags = { Name = "${local.name}-alb" }
}

resource "aws_lb_target_group" "public" {
  for_each = toset(["web", "pulse-api"])

  name        = "${local.name}-${each.key}"
  port        = local.service_ports[each.key]
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = each.key == "web" ? "/" : "/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.public.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.public["web"].arn
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.public["pulse-api"].arn
  }

  condition {
    path_pattern {
      values = ["/v1/*"]
    }
  }
}

variable "certificate_arn" {
  type        = string
  description = "ACM certificate for the dashboard hostname."
  default     = ""
}

# Scoring is the service that feels load, because a panel refresh reads a
# thirty day slice per patient. The others are steady.
resource "aws_appautoscaling_target" "scoring" {
  max_capacity       = var.environment == "production" ? 6 : 2
  min_capacity       = local.service_desired_count["scoring"]
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.services["scoring"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "scoring_cpu" {
  name               = "${local.name}-scoring-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.scoring.resource_id
  scalable_dimension = aws_appautoscaling_target.scoring.scalable_dimension
  service_namespace  = aws_appautoscaling_target.scoring.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 65
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

output "load_balancer_dns" {
  value = aws_lb.public.dns_name
}

output "ecr_repository_urls" {
  value = { for name, repo in aws_ecr_repository.services : name => repo.repository_url }
}
