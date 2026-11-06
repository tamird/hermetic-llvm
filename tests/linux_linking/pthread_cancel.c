#include <pthread.h>
#include <stddef.h>

static void *cancel_self(void *arg) {
  if (pthread_cancel(pthread_self()) == 0) {
    pthread_testcancel();
  }
  return arg;
}

int main(void) {
  pthread_t thread;
  if (pthread_create(&thread, NULL, cancel_self, NULL) != 0) {
    return 1;
  }
  void *result;
  return pthread_join(thread, &result) != 0 || result != PTHREAD_CANCELED;
}
