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

ENV PORT=8080
EXPOSE 8080

# Run as the built-in non-root user for defense-in-depth.
USER node

CMD ["node", "server/src/index.js"]
