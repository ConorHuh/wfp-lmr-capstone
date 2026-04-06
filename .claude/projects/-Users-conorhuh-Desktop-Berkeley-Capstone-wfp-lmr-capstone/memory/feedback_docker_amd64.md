---
name: Docker build must target linux/amd64
description: Always build Docker images with --platform linux/amd64 for ECS Fargate deployment from Apple Silicon Macs
type: feedback
---

Docker builds must use `--platform linux/amd64` when targeting ECS Fargate.

**Why:** User's Mac is Apple Silicon (ARM). Default `docker build` produces ARM images which fail on Fargate with `CannotPullContainerError: image Manifest does not contain descriptor matching platform 'linux/amd64'`.

**How to apply:** Any `docker build` command in deploy scripts or manual builds must include `--platform linux/amd64`.
