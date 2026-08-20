# Deployed release. Committed on purpose: the running version is then visible in
# git history, and a plain `tofu apply` cannot accidentally fall back to the
# non-existent :bootstrap placeholder.
#
# To release a new build: push the image, update this tag, commit, apply.
container_image = "540586745717.dkr.ecr.us-east-1.amazonaws.com/relational-fluency/platform:cfe8810"
