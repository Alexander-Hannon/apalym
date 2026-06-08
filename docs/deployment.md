Amplify URL: https://main.d2w8aq8p8rlibl.amplifyapp.com/

ALB URL: http://apalym-api-alb-19713019.us-east-1.elb.amazonaws.com/health

ECS Cluster
apalym-staging

ECS Service
apalym-ai-service

Target Group
apalym-api-tg

Health Endpoint
/health

Container Port
8000

----

ACM Certificate:
Status: Issued

Domain:
api.apalym.com

Date Issued:
6/5/26



----

## June 8 Progress

Completed:
- ECS service healthy
- Target group healthy
- ALB healthy
- ACM SSL certificate issued
- HTTPS listener (443) configured
- api.apalym.com DNS configured
- https://api.apalym.com/health returning healthy response

Outstanding:
- Amplify frontend still needs final redeploy using:
  NEXT_PUBLIC_API_BASE=https://api.apalym.com

Remaining validation:
- Generate Brief workflow
- Upload workflow
- Auth config endpoint
- Mixed-content verification
