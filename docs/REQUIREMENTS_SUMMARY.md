# Wymagania wyzwania (streszczenie własnymi słowami)

Streszczenie zespołu HYDRA NEXUS, HackNation 2026 (21-22.11.2026). To nie jest tekst organizatora; oficjalne
materiały zadania nie są częścią repozytorium.

**Cel.** Opracować metodę, która łączy zgłoszenia fałszywych reklam (incydenty) w kampanie na podstawie śladów
tekstowych, wizualnych, semantycznych i infrastrukturalnych. Nie chodzi o idealny detektor pojedynczej reklamy,
tylko o skuteczne i wytłumaczalne łączenie, które uwzględnia dostępne dane i wiarygodność powiązań.

**Co system ma umieć.**

1. Przyjąć różne wejścia: adres reklamy lub strony, zrzut ekranu, zapis rozmowy, plik multimedialny, zbiór danych.
2. Automatycznie zebrać cechy: tekst i słowa kluczowe, wizerunki osób i logotypy, cechy obrazu, domeny,
   przekierowania, linki, strukturę strony oraz publiczne metadane domen i infrastruktury.
3. Zamienić je na porównywalne cechy, wykrywać duplikaty i warianty tego samego przekazu.
4. Grupować incydenty w kampanie i grupy podobnych kampanii.
5. Ocenić nowy materiał względem znanego zbioru: przypisać do kampanii albo oznaczyć jako nowy wzorzec.
6. Pokazać powiązania czytelnie (klastry, graf, lista podobnych incydentów) wraz z cechami i uzasadnieniem.
7. Działać przy niepełnych danych (niedostępne strony, brakujące materiały).

**Bonus.** Automatyczne wyszukiwanie podejrzanych reklam w publicznych źródłach (np. bibliotekach reklam),
okresowe aktualizacje i wskazywanie nowych materiałów powiązanych ze znanymi kampaniami.

**Wymagania formalne.** Działający demonstrator z prostym GUI, opis metodyki, instrukcja instalacji, konfiguracji
i uruchomienia z zależnościami, kod w repozytorium Git, mile widziany kontener. Usługi zewnętrzne wymagające
dostępu muszą być opisane tak, by ocena była możliwa bez prywatnych kluczy. Wymagana lista bibliotek, modeli i
usług zewnętrznych z licencjami i limitami użycia ([LICENSES.md](LICENSES.md)). Dane organizatora służą tylko do
wyzwania i nie są publikowane.

Gdzie to jest w produkcie: [METHOD_MAP.md](METHOD_MAP.md), [METHODOLOGY.md](METHODOLOGY.md).
