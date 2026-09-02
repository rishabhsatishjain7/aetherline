variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ap-south-1"
}

variable "services" {
  description = "Service names, one ECR repository is created per entry"
  type        = list(string)
  default     = ["order-service", "inventory-service", "payment-service", "notification-service", "api-gateway"]
}

variable "image_retention_count" {
  description = "How many tagged images to retain per repository before lifecycle policy expires older ones"
  type        = number
  default     = 10
}
