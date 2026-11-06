#include <resolv.h>
#include <string.h>

int main(void) {
  const unsigned char byte = 'A';
  char encoded[5];
  return b64_ntop(&byte, 1, encoded, sizeof(encoded)) != 4 ||
         strcmp(encoded, "QQ==") != 0;
}
