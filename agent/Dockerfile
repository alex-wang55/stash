FROM public.ecr.aws/lambda/python:3.13

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY stash_agent/ ${LAMBDA_TASK_ROOT}/stash_agent/
COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
