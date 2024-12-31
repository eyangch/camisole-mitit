from camisole.models import Lang, Program

class Text(Lang, name="text"):
    source_ext = '.txt'
    interpreter = Program('cat', opts=[])
    reference_source = r'42'