# ai_connector in Docker

Runs the module in a misp-modules server on port 6667, on the Docker network of
[misp-docker](https://github.com/MISP/misp-docker). It publishes no port on the host: only
containers on that network (MISP, zsazsa, ...) reach it, as `http://ai-connector:6667`.

```bash
docker compose -f docker/docker-compose.yml up -d --build     # from the repository root
docker compose -f docker/docker-compose.yml ps                # healthy after a few seconds
```

- **Network:** `misp-docker_default` by default; for another one, `MISP_NETWORK=<name>` in front
  of the commands. It must exist first (start MISP).
- **MISP:** on the *AI* settings tab, `Plugin.AI_services_url = http://ai-connector` and
  `Plugin.AI_services_port = 6667`; *Test LLM* checks the whole path.
- **Settings:** the repository's `.env` is passed in as environment if it exists
  (docs/OPERATOR_GUIDE.md 1.3). It is never copied into the image. Change it, then
  `docker compose -f docker/docker-compose.yml up -d` again.
- **Logs:** `docker compose -f docker/docker-compose.yml logs -f`.
- **Upgrade:** `git pull`, then the `up -d --build` line again.
