#!/usr/bin/env python3
"""
Flux — ouvre une RUN pour un workflow.

Le runtime ne travaille que dans un workspace existant : `resolve_ws` cherche le
dossier le plus récent portant un `WORKSPACE.txt` sous le `workspace_root` du
workflow. La veille crée ce dossier depuis son premier nœud (veille-start) ; les
workflows dont le premier nœud écrit DÉJÀ dans le workspace (pipeline web app)
ont besoin qu'on l'ouvre avant. C'est ce que fait ce script.

Usage :
  new-run.py <workflow-id> <slug> [--depuis DIR] [--note TEXTE]
             [--mode banc|production] [--cas <id>] [--champ cle=valeur]...

  --depuis  copie un projet existant comme point de départ (git clone si c'est
            une URL, copie récursive si c'est un chemin)
  --note    ligne libre ajoutée à WORKSPACE.txt
  --mode    banc (un cas connu, chaque étape notée) ou production (entrée
            neuve, pas de note). Défaut : banc si --cas est donné, sinon
            production. Une run ne change pas de mode en cours (D6).
  --cas     le cas de banc (`cas/<id>/`) : ses fichiers d'ENTRÉE (ceux que
            les nœuds déclarent dans `entree` et qu'aucun nœud ne produit)
            sont copiés dans le workspace ; le corrigé reste dans cas/.
  --champ   un paramètre de run (`parametres_run` du graphe). Un paramètre de
            type fichier est copié dans le workspace sous son id.

Écrit `state/<wf>/<run>/run.json` : {workflow, run, workspace, mode, cas,
statut: en-cours, parametres, ouvert} — l'état de la run, que le séquenceur
et la garde font évoluer (statut en-cours | bloque | terminee | close).

Sortie : le chemin du workspace créé, sur stdout.
"""
import sys, os, json, re, shutil, argparse, pathlib, datetime, subprocess, hashlib, unicodedata

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import socle

FLUX = socle.racine()


def slugify(s):
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s.strip().lower()).strip('-')
    return s or 'run'


# LES EMPREINTES SE LISENT SANS OUVRIR LE COFFRE. Le lot de mesure est en 0700
# pour un groupe sans membre (verrou c du design) : l'utilisateur de la boucle
# ne peut pas le lire, et c'est VOULU — une boucle qui lit les corriges qu'on
# la mesure a reproduire ne se mesure plus. Cette couche-la n'a jamais eu
# besoin du CONTENU des cas, seulement de savoir le RECONNAITRE s'il se
# presente ailleurs : elle travaille donc sur un manifeste d'empreintes
# IRREVERSIBLES, pose a cote du coffre et lisible par tous parce qu'il ne dit
# rien — un sha256 ne se remonte pas en texte, il ne fait que comparer.
MANIFESTE_EMPREINTES = (os.environ.get('FLUX_EMPREINTES')
                        or '/var/lib/flux-empreintes/empreintes.json')
_EMPREINTE_DU_VIDE = hashlib.sha256(b'').hexdigest()


def _normaliser(t):
    """Le texte tel que le manifeste le hache : NFD sans accents, minuscules,
    espaces reduits. L'empreinte survit ainsi a une recopie qui reindente, qui
    recoupe les lignes ou qui perd les accents."""
    t = unicodedata.normalize('NFD', t).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'\s+', ' ', t).strip()


def _empreintes_du_fichier(f):
    """Les empreintes d'un fichier, dans la forme exacte du manifeste. Un
    fichier illisible, vide, ou blanc une fois normalise n'en donne aucune : il
    ne prouverait rien."""
    try:
        brut = f.read_bytes()
    except OSError:
        return ()
    if not brut:
        return ()
    try:
        e = hashlib.sha256(_normaliser(brut.decode('utf-8')).encode('utf-8')).hexdigest()
    except UnicodeDecodeError:
        e = hashlib.sha256(brut).hexdigest()          # binaire : l'octet fait foi
    return () if e == _EMPREINTE_DU_VIDE else (e,)


def _ids_declares_en_mesure():
    """Les ids que la carte du depot range dans le lot de mesure."""
    try:
        carte = json.loads((FLUX / 'cas' / 'cas.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    return {c.get('id') for c in (carte.get('cas') or []) if c.get('lot') == 'mesure'}


def empreintes_du_lot_de_mesure():
    """{empreinte d'un fichier -> id du cas de mesure qui le porte}.

    Ce qui trahit un cas de mesure, c'est son CONTENU — le mail, les deux
    corriges — pas le nom du dossier qui le porte : un nom se renomme en une
    commande, un contenu non.

    LE MANIFESTE FAIT AUTORITE des qu'il couvre tous les cas que la carte
    declare en mesure : alors le coffre n'est pas ouvert, ni meme regarde, et
    la reconnaissance par contenu marche sous N'IMPORTE QUEL utilisateur — y
    compris celui de la boucle, a qui le lot est ferme. C'est le renversement
    par rapport a la version qui indexait le lot en le lisant : cette couche
    n'etait alors qu'un bonus du lanceur privilegie, et elle rendait un index
    VIDE sous l'utilisateur de la boucle — c'est-a-dire precisement la ou elle
    protege. Les autres couches ne la remplacent pas : un cas du depot n'a pas
    de `cas.json` (rien a marquer) et, dans le lot, ni `mail.md` ni les deux
    corriges ne portent de temoin (0 sur 15) — un cas de boucle declare dont on
    aurait remplace le mail par celui d'un cas confine passait donc entier.

    On ne retombe sur une lecture directe du coffre que si le manifeste ne fait
    pas autorite (absent, corpus jetable des tests, cas de mesure ajoute depuis
    sa generation) — lecture tentee sans y croire et JAMAIS fatale : ne pas
    avoir le droit de lire le lot est l'etat attendu, pas une panne.

    Seules les empreintes de FICHIER ENTIER entrent ici. Les `phrases` du
    manifeste servent a traquer une fuite dans une sortie ; comme gardien
    d'ouverture elles feraient des faux positifs — 18 fichiers du lot boucle
    partagent au moins une phrase avec un cas de mesure (formules de politesse,
    entetes de corrige) — et un faux positif bloquerait un cas de boucle
    parfaitement legitime."""
    index = {}
    try:
        manifeste = json.loads(pathlib.Path(MANIFESTE_EMPREINTES)
                               .read_text(encoding='utf-8')).get('cas') or {}
    except (OSError, ValueError, AttributeError):
        manifeste = {}
    for ident, d in manifeste.items():
        for emp in ((d or {}).get('fichiers') or {}).values():
            if emp and emp != _EMPREINTE_DU_VIDE:
                index.setdefault(emp, ident)
    if manifeste and _ids_declares_en_mesure() <= set(manifeste):
        return index                 # le manifeste couvre le lot : coffre inutile
    racine_cas = socle.racine_mesure() / 'cas'
    try:
        entrees = sorted(racine_cas.iterdir())
    except OSError:                  # absent, ou illisible : voir ci-dessus
        return index
    for d in entrees:
        try:
            if not d.is_dir():
                continue
            fichiers = sorted(d.rglob('*'))
        except OSError:
            continue
        for f in fichiers:
            try:
                fichier = f.is_file()
            except OSError:
                continue
            if fichier:
                for emp in _empreintes_du_fichier(f):
                    index.setdefault(emp, d.name)
    return index


def cas_de_mesure_par_contenu(cas_dir):
    """L'id du cas de mesure dont `cas_dir` porte le contenu, ou None.

    Un seul fichier commun suffit a conclure : les fichiers du lot confine sont
    deux a deux distincts et aucun ne se retrouve dans un cas de boucle
    (verifie a la pose : 75 empreintes, 0 collision). Les fichiers vides sont
    ignores — ils ne prouveraient rien."""
    index = empreintes_du_lot_de_mesure()
    if not index:
        return None
    try:
        fichiers = sorted(cas_dir.rglob('*'))
    except OSError:
        return None
    for f in fichiers:
        try:
            fichier = f.is_file()
        except OSError:
            continue
        if not fichier:
            continue
        for emp in _empreintes_du_fichier(f):
            ident = index.get(emp)
            if ident:
                return ident
    return None


def cas_de_mesure_par_marque(cas_dir, ids_de_mesure):
    """L'id du cas de mesure que ce dossier DIT etre, ou None.

    Un cas de mesure se declare dans son propre `cas.json` : `lot: mesure`, un
    `temoin` unique, un `avertissement`. Cette carte de visite voyage avec le
    dossier — une copie recursive l'emporte — la ou le nom, lui, se change. Et
    elle se lit sans aucun privilege, donc y compris par l'utilisateur de la
    boucle, a qui le lot est ferme.

    Les cas du depot, eux, n'ont pas de `cas.json` du tout ; celui qu'ecrit
    `POST /valider` porte `origine: valider-corriger`, ni lot, ni temoin."""
    try:
        d = json.loads((cas_dir / 'cas.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    ident = str(d.get('id') or cas_dir.name)
    if d.get('lot') == 'mesure' or d.get('temoin') or ident in ids_de_mesure:
        return ident
    return None


# Un temoin d'etancheite : `FLXM` + 8 caracteres, unique par cas de mesure.
# La boucle ne connait AUCUN de ces quinze mots — ils vivent dans le lot, qui
# lui est ferme — mais elle en connait la FORME, et c'est tout ce qu'il faut
# pour reconnaitre un fichier du lot qui se serait egare ici. Elle ne les
# imprime jamais : un temoin recopie dans un message d'erreur atterrirait dans
# les journaux de la console, c'est-a-dire exactement la contamination qu'il
# sert a detecter.
MOTIF_TEMOIN = re.compile(r'FLXM[0-9A-Z]{8}')
TAILLE_MAX_LUE = 2 << 20


def porte_un_temoin_de_mesure(cas_dir):
    """Un fichier de ce dossier porte-t-il un temoin du lot de mesure ?

    Les notes d'un cas de mesure en portent un, son cas.json aussi. Un dossier
    du depot n'en porte aucun — tools/verifier-etancheite.sh (controle 7) en
    fait meme sa preuve de non-contamination."""
    try:
        fichiers = sorted(cas_dir.rglob('*'))
    except OSError:
        return False
    for f in fichiers:
        try:
            if not f.is_file() or f.stat().st_size > TAILLE_MAX_LUE:
                continue
            if MOTIF_TEMOIN.search(f.read_text(encoding='utf-8', errors='replace')):
                return True
        except OSError:
            continue
    return False


def _existe(chemin):
    """`chemin.exists()` qui ne leve pas. Sur un lien vers un dossier illisible
    — un cas confine vu depuis l'utilisateur de la boucle — `Path.exists()`
    remonte PermissionError au lieu de rendre False."""
    try:
        return chemin.exists()
    except OSError:
        return False


def lieu_du_cas(cas_dir, reel):
    """D'ou ce cas se presente-t-il : 'depot', 'mesure', ou None ?

    UN CAS N'HABITE QUE DEUX ENDROITS : `cas/<id>/` du depot (lot de boucle,
    sentinelle, cas nes de `POST /valider`) et `<racine de mesure>/cas/<id>/`
    (lot confine). Un dossier qui se presente d'ailleurs — /tmp, un point de
    montage, le repertoire d'un autre projet — n'est pas un cas : c'est une
    copie posee la, et c'est par ce chemin qu'un cas confine repasserait dans
    le monde public sans qu'aucune regle de nom le voie.

    On regarde le chemin RESOLU : un lien pose dans `cas/` qui mene ailleurs
    n'est pas « dans le depot », il est la ou il mene."""
    racine_depot = FLUX / 'cas'
    try:
        if reel.parent == racine_depot.resolve():
            return 'depot'
    except (OSError, RuntimeError):
        pass
    try:
        if (socle.racine_mesure() / 'cas').resolve() in reel.parents:
            return 'mesure'
    except (OSError, RuntimeError):
        pass
    return None


def _depuis_le_depot(cas_dir):
    """Le cas est-il PRESENTE depuis le `cas/` du depot ? La question se pose
    AVANT toute resolution : un lien symbolique pose la, justement, se presente
    depuis le depot — et apparait dans le selecteur de la console."""
    return (FLUX / 'cas') in cas_dir.parents


def lot_du_cas(cas):
    """Le lot d'un cas (`boucle`, `mesure`, `sentinelle`), son dossier, son id
    (None si on sait seulement que c'en est un) et le MOTIF qui a decide.

    Un cas de mesure vit sous /var/lib/flux-mesure/cas/ et NULLE PART ailleurs.
    Cinq lectures, parce qu'un nom de dossier se change et pas ce qu'il y a
    dedans — de la plus forte a la plus faible :
      chemin  le dossier, liens suivis, est sous la racine de mesure : cas de
              mesure, quel que soit le nom sous lequel il se presente ;
      marque  son propre `cas.json` le declare (lot, temoin) : c'est une COPIE
              d'un cas de mesure, et cette lecture ne demande aucun privilege ;
      temoin  un de ses fichiers porte un temoin d'etancheite : meme conclusion,
              sans meme savoir lequel des quinze cas c'est ;
      carte   `cas/cas.json` donne le lot de l'id — c'est le nom du dossier,
              donc la seule chose qu'un renommage suffit a effacer ;
      contenu un de ses fichiers a l'empreinte d'un fichier du coffre — par le
              manifeste public, donc sous n'importe quel utilisateur.

    Rend aussi le LIEU d'ou le cas se presente (voir `lieu_du_cas`) : c'est lui
    qui distingue un cas du depot d'une copie posee n'importe ou.
    """
    cas_dir = pathlib.Path(cas) if os.path.isabs(cas) else FLUX / 'cas' / cas
    ident = cas_dir.name
    lot, ids_de_mesure, declares = None, set(), set()
    try:
        carte = json.loads((FLUX / 'cas' / 'cas.json').read_text(encoding='utf-8'))
        for c in carte.get('cas') or []:
            declares.add(c.get('id'))
            if c.get('lot') == 'mesure':
                ids_de_mesure.add(c.get('id'))
            if c.get('id') == ident:
                lot = c.get('lot')
    except (OSError, ValueError):
        lot, declares = None, set()
    try:
        # resolve() ne fait que suivre les liens : elle reussit meme si la cible
        # est illisible, donc cette couche tient aussi sous l'utilisateur boucle.
        reel = cas_dir.resolve()
    except (OSError, RuntimeError):
        reel = cas_dir
    lieu = lieu_du_cas(cas_dir, reel)
    etat = {'lieu': lieu, 'reel': reel, 'declare': ident in declares,
            'marque': _existe(cas_dir / 'cas.json')}
    if lieu == 'mesure':
        return 'mesure', cas_dir, reel.name, 'chemin', etat
    if lot != 'mesure':
        jumeau = cas_de_mesure_par_marque(cas_dir, ids_de_mesure)
        if jumeau:
            return 'mesure', cas_dir, jumeau, 'marque', etat
        if porte_un_temoin_de_mesure(cas_dir):
            return 'mesure', cas_dir, None, 'temoin', etat
        jumeau = cas_de_mesure_par_contenu(cas_dir)
        if jumeau:
            return 'mesure', cas_dir, jumeau, 'contenu', etat
    return lot, cas_dir, ident, 'carte', etat


def refuser_cas_illegitime(a):
    """LE REFUS NET, sur ce que le dossier EST et non sur le nom qu'il porte.

    Deux refus, pour un seul et meme danger — un cas de mesure joue depuis le
    depot deverserait son mail dans le workspace public et les `attendu=` du
    juge dans `state/`, que la console publie sans authentification et que la
    sauvegarde emporte ; le cas serait brule :

      1. le dossier se presente d'un lieu inconnu (ni `cas/` du depot, ni la
         racine de mesure) : on ne joue pas un dossier pose la ;
      2. il est, ou il contient, un cas du lot de mesure — reconnu par son
         chemin resolu, sa marque, un temoin, la carte ou son empreinte.

    Un cas du depot doit en outre etre DECLARE : dans `cas/cas.json`, ou par un
    `cas.json` a lui (ce qu'ecrit `POST /valider`). Sans quoi un dossier depose
    dans `cas/` — le geste exact d'une copie egaree — s'ouvrirait sur la seule
    foi de son emplacement.

    Rend le dossier de mesure quand la demande est legitime (son chemin
    confine, hors du depot), None quand le cas est un cas du depot en regle."""
    if not a.cas:
        return None
    lot, cas_dir, ident, motif, etat = lot_du_cas(a.cas)
    racine_cas = socle.racine_mesure() / 'cas'
    if lot == 'mesure':
        if motif == 'chemin' and not _depuis_le_depot(cas_dir):
            return cas_dir                  # la seule facon legitime de le jouer
        detail = {
            'carte': "%s est un cas du LOT DE MESURE (cas/cas.json)." % ident,
            'chemin': "%s mene au cas de mesure %s (lien suivi) et se presente depuis le depot."
                      % (a.cas, ident),
            'marque': "%s porte la MARQUE du cas de mesure %s (son propre cas.json) :\n"
                      "c'est une copie, et le nom n'y change rien." % (a.cas, ident),
            'temoin': "%s porte un TEMOIN d'etancheite du lot de mesure :\n"
                      "c'est une copie d'un cas confine, et le nom n'y change rien." % (a.cas,),
            'contenu': "%s porte le CONTENU du cas de mesure %s (meme empreinte sha256) :\n"
                       "c'est une copie, et le nom n'y change rien." % (a.cas, ident),
        }[motif]
        ou = (("Joue-le confine, par son chemin de mesure :\n"
               "    tools/jouer-cas.sh %s/%s <modele>   (FLUX_MESURE_AUTORISEE=1)\n"
               "    (ou --cas %s)" % (racine_cas, ident, racine_cas / ident)) if ident else
              ("Efface cette copie : le lot de mesure se joue depuis %s, et de la seule."
               % racine_cas))
        sys.exit(
            "refus : %s\n"
            "Il ne s'ouvre pas depuis le depot : sa run y deverserait le mail et\n"
            "les `attendu=` du juge, que la console publie sans authentification.\n"
            "%s" % (detail, ou))
    if etat['lieu'] is None:
        sys.exit(
            "refus : %s ne vient d'aucun lieu de cas connu (resolu : %s).\n"
            "Un cas habite `cas/<id>/` du depot, ou `%s/<id>/` pour le lot\n"
            "confine — nulle part ailleurs. Un dossier pose ailleurs et joue depuis\n"
            "le depot est le chemin exact par lequel un cas de mesure repasse en\n"
            "public : on ne l'ouvre pas. Range-le dans cas/ et declare-le."
            % (a.cas, etat['reel'], racine_cas))
    if etat['lieu'] == 'depot' and not etat['declare'] and not etat['marque']:
        sys.exit(
            "refus : %s n'est declare nulle part (ni dans cas/cas.json, ni par un\n"
            "cas.json a lui). Un dossier depose dans cas/ ne devient pas un cas parce\n"
            "qu'il est la : c'est ainsi qu'une copie egaree se ferait jouer.\n"
            "Declare-le dans cas/cas.json, ou passe par\n"
            "POST /valider, qui ecrit le cas.json du cas qu'il cree." % a.cas)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workflow')
    ap.add_argument('slug')
    ap.add_argument('--depuis', default=None)
    ap.add_argument('--note', default=None)
    ap.add_argument('--champ', action='append',
                    help='valeur d\'un champ de l\'entête, cle=valeur (répétable)')
    ap.add_argument('--mode', choices=['banc', 'production'], default=None)
    ap.add_argument('--cas', default=None, help='cas de banc : dossier cas/<id>/')
    a = ap.parse_args()
    _vals0 = dict(x.split('=', 1) for x in (a.champ or []) if '=' in x)
    if not a.cas and _vals0.get('cas'):
        a.cas = _vals0['cas']                    # `--champ cas=<id>` vaut `--cas <id>`
    if a.mode is None:
        a.mode = _vals0.get('mode') if _vals0.get('mode') in ('banc', 'production') else None
    if a.mode is None:
        a.mode = 'banc' if a.cas else 'production'
    if a.mode == 'banc' and not a.cas:
        sys.exit('une run en banc demande un cas (--cas <id>) : sans corrigé, rien à noter')

    p = socle.fichier_workflow(a.workflow)
    if not p.exists():
        sys.exit(f'workflow introuvable: {p}')
    wf = json.loads(p.read_text(encoding='utf-8'))
    # LA RACINE SUIT LE LOT. Une run de mesure n'ecrit pas une ligne sous
    # <FLUX_RUNS_HOME>/<wf>-runs (que la console liste) ni sous state/ (que la
    # console sert et que restic emporte) : elle vit sous la racine de mesure,
    # en 0700, et `socle.dossier_run` y emmene son etat toute seule.
    cas_mesure = refuser_cas_illegitime(a)
    if cas_mesure is not None:
        root = socle.racine_runs_mesure() / 'ws'
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except PermissionError:
            # Le lot confine appartient a un autre utilisateur que la boucle
            # (verrou c du design) : c'est voulu, et ca se dit ici clairement
            # plutot qu'en trace d'exception.
            try:
                proprio = socle.racine_mesure().owner()
            except Exception:
                proprio = 'le proprietaire du lot'
            sys.exit("refus : %s n'est pas ouvrable par l'utilisateur courant (uid %d).\n"
                     "Une run de mesure s'ouvre sous l'identite qui possede le lot :\n"
                     "    sudo -u %s python3 runtime/new-run.py ..." % (root, os.getuid(), proprio))
        for e in (root, root.parent):
            try:
                os.chmod(e, 0o700)
            except OSError:
                pass
    else:
        root = socle.workspace_root(wf)
        root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')
    ws = root / f'{slugify(a.slug)}-{stamp}'
    if ws.exists():
        sys.exit(f'workspace déjà présent: {ws}')

    if a.depuis and re.match(r'(https?://|git@)', a.depuis):
        subprocess.check_call(['git', 'clone', '--quiet', a.depuis, str(ws)])
    elif a.depuis:
        src = pathlib.Path(a.depuis)
        if not src.is_dir():
            sys.exit(f'source introuvable: {src}')
        shutil.copytree(src, ws)
    else:
        ws.mkdir(parents=True)
    if cas_mesure is not None:
        try:
            os.chmod(ws, 0o700)
        except OSError:
            pass

    (ws / 'docs' / 'pipeline').mkdir(parents=True, exist_ok=True)

    lignes = [
        f'workflow: {wf["id"]}',
        f'projet: {a.slug}',
        f'ouvert: {datetime.datetime.now().isoformat(timespec="seconds")}',
    ]
    lignes.append(f'mode: {a.mode}')
    if a.cas:
        lignes.append(f'cas: {a.cas}')
    if a.depuis:
        lignes.append(f'depuis: {a.depuis}')
    if a.note:
        lignes.append(f'note: {a.note}')

    # L'ENTÊTE QUE LA PIPELINE RÉCLAME. Une pipeline déclare dans `ouverture`
    # ce que ses nœuds vont lire dans le WORKSPACE.txt — pour la veille, le
    # `owner`, qui dit à QUI la livraison s'adresse et donc quel profil charger.
    # Le point d'entrée web remplissait ce gabarit ; ce script, non. Une run
    # ouverte en ligne de commande partait donc sans destinataire, et l'étape 1
    # ne retrouvait pas le silo — le défaut ne se voyait qu'au premier nœud.
    #
    # Les valeurs viennent de `--champ cle=valeur` ; celles qui manquent se
    # DÉDUISENT du slug, en le relisant à travers le gabarit `ouverture.slug`.
    ouv = wf.get('ouverture') or {}
    if ouv.get('entete'):
        vals = dict(x.split('=', 1) for x in (a.champ or []) if '=' in x)
        gabarit = ouv.get('slug') or ''
        cles = re.findall(r'\{(\w+)\}', gabarit)
        if cles and not all(k in vals for k in cles):
            motif = '^' + re.escape(gabarit).replace('\\{', '{').replace('\\}', '}')
            for k in cles:
                motif = motif.replace('{' + k + '}', f'(?P<{k}>.+?)')
            # Un slug comme « ia-generaliste-jean-dupont-example-org » se coupe
            # a beaucoup d'endroits, et toutes les coupes donnent une lecture
            # plausible : deviner produirait un destinataire qui n'existe pas,
            # ce qui est PIRE que pas de destinataire du tout — un profil
            # introuvable s'arrete net, un mauvais profil livre a quelqu'un
            # d'autre. On essaie donc TOUTES les coupes et on ne retient que
            # celle dont l'`owner` est un silo reellement installe. Aucune ne
            # convient : on refuse, et on demande les champs.
            silos = set()
            try:
                silos = {d.name for d in
                         pathlib.Path(os.environ.get('FLUX_OWNERS_DIR') or
                                      (socle.runs_home() / 'owners')).iterdir()
                         if d.is_dir()}
            except Exception:
                pass
            if cles == ['subject', 'owner'] and gabarit == '{subject}-{owner}':
                bouts = a.slug.split('-')
                for i in range(1, len(bouts)):
                    suj, own = '-'.join(bouts[:i]), '-'.join(bouts[i:])
                    if not silos or own in silos:
                        vals.setdefault('subject', suj)
                        vals.setdefault('owner', own)
                        break
            else:
                m = re.match(motif + '$', a.slug)
                if m:
                    for k, v in m.groupdict().items():
                        vals.setdefault(k, v)

        vals.setdefault('stamp', stamp)
        vals.setdefault('base', str(ws))
        vals.setdefault('iso', datetime.datetime.now().isoformat())
        vals.setdefault('notes', a.note or '')
        manque = [k for k in re.findall(r'\{(\w+)\}', ouv['entete']) if k not in vals]
        if manque:
            sys.exit('champs manquants pour l\'entête : ' + ', '.join(manque)
                     + ' — passe-les avec --champ cle=valeur')
        for l in ouv['entete'].rstrip('\n').split('\n'):
            for k, v in vals.items():
                l = l.replace('{' + k + '}', str(v))
            if l.strip():
                lignes.append(l)
    # WORKSPACE.txt sert aussi de REPÈRE DE FRAÎCHEUR : les gates refusent tout
    # livrable plus ancien que lui (approbation, bundle, rapports hérités).
    wsf = ws / 'WORKSPACE.txt'
    wsf.write_text('\n'.join(lignes) + '\n', encoding='utf-8')

    # WORKSPACE.txt est le REPÈRE DE FRAÎCHEUR des gates : un livrable plus ancien que
    # lui passe pour hérité d'un projet précédent. À l'import, le contenu accueilli
    # préexiste forcément — on recule donc le repère avant le plus ancien fichier importé,
    # sinon un travail parfaitement valide serait refusé pour la seule raison qu'on vient
    # de le copier.
    if a.depuis:
        plus_ancien = min(
            (f.stat().st_mtime for f in ws.rglob('*') if f.is_file() and f != wsf),
            default=None)
        if plus_ancien is not None:
            os.utime(wsf, (plus_ancien - 60, plus_ancien - 60))

    # LE CAS DE BANC ET LES PARAMETRES DE RUN entrent dans le workspace : les
    # fichiers d'entree du cas (ceux qu'un nœud lit et qu'aucun ne produit),
    # jamais son corrige ni son juge, qui restent sous cas/.
    vals = dict(x.split('=', 1) for x in (a.champ or []) if '=' in x)
    entrees_cas = []
    if a.cas:
        cas_dir = pathlib.Path(a.cas) if os.path.isabs(a.cas) else FLUX / 'cas' / a.cas
        try:
            lisible = cas_dir.is_dir()
        except OSError as e:
            sys.exit('cas illisible : %s (%s) — le lot confine ne se lit pas depuis '
                     'l\'utilisateur de la boucle' % (cas_dir, e.strerror))
        if not lisible:
            sys.exit(f'cas introuvable : {cas_dir}')
        produits = {l for n in wf['nodes'] for l in (n.get('livrables') or [])}
        for n in wf['nodes']:
            for e in (n.get('entree') or []):
                src = cas_dir / e
                if e in produits or not src.exists():
                    continue
                dst = ws / e
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
                entrees_cas.append(e)
    if a.cas:
        vals.setdefault('cas', a.cas)
    for prm in (wf.get('parametres_run') or []):
        pid_ = prm.get('id')
        if prm.get('mode') and prm.get('mode') != a.mode:
            continue                             # un parametre de l'autre mode
        v = vals.get(pid_)
        if v is None and prm.get('defaut') is not None:
            vals[pid_] = v = prm['defaut']
        if prm.get('requis') and v in (None, '') and prm.get('type') != 'fichier':
            sys.exit(f'parametre de run manquant : {pid_} ({prm.get("label") or pid_}) — --champ {pid_}=<valeur>')
        if prm.get('type') == 'fichier' and v and os.path.exists(v):
            dst = ws / (prm.get('destination') or pid_)
            if os.path.isdir(v):
                shutil.copytree(v, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(v, dst)
    (ws / 'state').mkdir(exist_ok=True)
    socle.ecrire_run(wf['id'], str(ws), workflow=wf['id'], run=ws.name, workspace=str(ws),
                     mode=a.mode, cas=a.cas, statut='en-cours', parametres=vals,
                     entrees_du_cas=entrees_cas, ouvert=datetime.datetime.now().isoformat(timespec='seconds'),
                     noeud_courant=None)

    try:
        shutil.chown(ws, 'claude', 'claude')
        for d, _, fs in os.walk(ws):
            shutil.chown(d, 'claude', 'claude')
            for f in fs:
                shutil.chown(os.path.join(d, f), 'claude', 'claude')
    except (LookupError, PermissionError, OSError):
        pass

    print(ws)


if __name__ == '__main__':
    main()
