#!/usr/bin/env bash
# Un faux modele de garde : lit le prompt sur stdin, repond toujours « alerter ».
cat > /dev/null
echo '{"geste": "alerter", "motif": "faux modele de garde : alerte de test"}'
