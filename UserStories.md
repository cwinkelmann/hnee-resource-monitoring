# User Workflows or Tasks 
User Resource management Workflows on a 8 GPU, 224 CPU, 1TB RAM server

## User Alice needs for GPUS for a script 
It runs a deep learning model. She wants to book a GPU for a specific time window, run her script, and then release the GPU when done.

Bob already has a booking since 1h and for the next 6 hours for 5 GPUs. 

The bookigng should fail for alice because the requests on GPU too much. She can either book her GPUs later or book less.

## Alice has scripts which need 4 GPUS uninterrupted

She does not care when the script runs so her script tries to book and her scripts checkes/gets notified when the resources become available.

## Alice book 40GB, 40GB remain free
Assumince 7 GPUs are fully booked and 1 GPU has 40GB free Bbob should be able of running

## Alice needs run 13000 jobs (13000 GPU hours)
She would want to book all GPUS individually but of course anyone else should have a chance of using the GPUS before the jobs are done. So she configures the lowest priority so others can even cancel the running scripts. So another user would effictevely kill the Process of Alice and take the GPU for their own jobs. The job queue of Alice would continue on her own when the bookings by others are released. SInce she booked just 10GPUs someone else couse use the GPus in parallel if their booking requires less than what is availabe.
