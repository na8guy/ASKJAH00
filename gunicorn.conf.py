import multiprocessing
import os

bind = "0.0.0.0:" + str(os.getenv("PORT", "8080"))
workers = multiprocessing.cpu_count() * 2 + 1
threads = 2
timeout = 30
accesslog = "-"
errorlog = "-"
loglevel = "info"