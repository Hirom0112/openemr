# syntax=docker/dockerfile:1.7

# Multi-stage build for the patient-dashboard Next.js app.
# Targets Railway / any container host. Producing a standalone bundle
# keeps the runtime image small and avoids shipping devDependencies.

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Next.js standalone output mode produces a minimal server bundle.
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# Non-root user for the runtime.
RUN addgroup --system --gid 1001 nodejs \
  && adduser --system --uid 1001 nextjs

# With `output: "standalone"` in next.config.ts, Next emits a
# self-contained ./.next/standalone tree containing server.js + only
# the runtime deps it needs. server.js correctly honours basePath
# (which `next start` doesn't in our test). Static + public assets
# must be copied alongside since standalone doesn't include them.
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
# server.js is the entrypoint emitted by standalone output. It binds
# to PORT env (3000) and HOSTNAME (0.0.0.0) and respects basePath.
CMD ["node", "server.js"]
