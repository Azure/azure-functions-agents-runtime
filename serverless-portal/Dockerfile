# Combined image for the Serverless Agent Portal.
#
# The Vite SPA is built locally (`npm run build`) and shipped PREBUILT in
# app/frontend/dist. The CoreAI design-system dependency (@coreai/fluentui-react)
# lives on a private Azure Artifacts feed that the image builder can't
# authenticate to, so the SPA build is intentionally NOT run inside the image.
# The single stage runs the Express server, which serves the prebuilt SPA from
# ../frontend/dist and proxies /api. Listens on :8080.

FROM node:22-alpine AS runtime
ENV NODE_ENV=production
WORKDIR /app

# Install production server dependencies only (all public npm).
COPY app/server/package*.json ./server/
RUN cd server && npm ci --omit=dev

# Server source + the prebuilt SPA. The server resolves DIST_DIR as
# <server>/../../frontend/dist, so the layout below must be preserved.
COPY app/server/ ./server/
COPY app/frontend/dist ./frontend/dist

# Runtime samples the portal enumerates via /api/samples. Copied from a
# pre-staged bundle inside serverless-portal/ so the image doesn't need
# access outside its build context.
COPY samples-bundle/ ./samples/

# The server writes portal-side drafts, app-sources scratch, and per-app deploy
# history under /app/server/.data. COPY ran as root, so we pre-create the
# subtree and hand it to the non-root user before switching.
RUN mkdir -p /app/server/.data && chown -R node:node /app/server/.data

ENV PORT=8080
EXPOSE 8080

# Run as the built-in non-root user for defense-in-depth.
USER node

CMD ["node", "server/src/index.js"]
