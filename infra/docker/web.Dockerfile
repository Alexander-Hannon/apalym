FROM node:20-alpine

WORKDIR /app

# We'll use npm for simplicity (no global tooling needed)
EXPOSE 3000

# Dev command assumes Next.js exists in /app
CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0", "--port", "3000"]
